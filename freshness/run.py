"""The freshness bot runner: one entry point for both halves (FRS-16, FRS-26).

    python3 -B -m freshness.run --half actions|mac [--dry-run] [--root .]

Contract: docs/DESIGN.md 7.6 (runtime split, dwell limits), 7.8 (who sends
what), 4.1 (workflow rules) and 5 (bot credentials).

- `--half actions` (FRS-26) runs in the nightly GitHub Actions job and needs
  no local CLI: the upstream watch (part 3), every CLI-free per-skill check
  of DESIGN 7.2 over every folder under `skills/` and `.held/`, `zz-canary`
  included (part 2), the age lifecycle and dwell limits (part 6) and the
  canary verification (part 8). It only opens issues and PRs.
- `--half mac` runs from launchd on the Mac: the CLI checks and held drift
  (part 4), the `claude -p` classifier for upstream changes the rules could
  not place (part 5), the Actions heartbeat, and every ops message (part 7).
- Both halves route their findings (part 5), plan alerts (part 1), apply
  the actions through part 5's writer, and write a heartbeat (FRS-16) to
  `state.heartbeats[half]` on the PR-less `freshness-state` branch. Each half
  raises `heartbeat-actions` or `heartbeat-mac` through part 6's dwell rows
  when the other half's `last_success` is older than 26 hours.

`--dry-run` prints the planned actions and writes nothing: no GitHub write,
no state commit, no ops message and no classifier call. A run that failed
(canary mismatch, a step error, an action GitHub refused, or a state write
that did not land) exits 1.

Credentials (DESIGN 5): the Actions half reads the App installation token
from `FRESHNESS_TOKEN` and the App's bot login from `FRESHNESS_BOT_LOGIN`,
both set by the workflow. The Mac half mints an installation token from the
App id and the private key in the macOS Keychain, signing the JWT with
`openssl dgst -sha256 -sign` (standard library only). The key reaches
openssl through a pipe, and neither the key nor the token is written to
disk, logged or printed.

State keys owned here: `heartbeats[half] = {"run_id", "start", "end",
"result", "counts", "last_success"}`, `heartbeat_watch[half]` (when this
half first saw the other with no success), `results[skill]` (per-skill
check results that `age.advance_actions` reads), `pr_seen[number]` (when a
freshness PR was first seen open, for the unreviewed-PR dwell row) and
`upstream_pending[fp]` (upstream findings waiting for the Mac classifier).

Every other module takes its network, GitHub and command access as
parameters; the real ones are built here, in `default_deps`, and nowhere
else. Python 3.9+, standard library only. Importing has no side effects.
"""

import argparse
import base64
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import age
from . import alerts as amod
from . import canary
from . import checks
from . import classify as cmod
from . import cli_checks
from . import dwell
from . import finding as fmod
from . import github as ghmod
from . import held_drift
from . import links
from . import notify
from . import route as rmod
from . import state as smod
from . import upstream

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, 'freshness', 'config.json')
LOCAL_CONFIG_PATH = '~/.config/skills-catalog/freshness.json'
EXCLUDE_PATH = '~/.config/skills-catalog/exclude.txt'
REPO = 'kennylhilljr/skills-catalog'
API = 'https://api.github.com'
USER_AGENT = 'skills-catalog-freshness'

HALVES = ('actions', 'mac')
OTHER = {'actions': 'mac', 'mac': 'actions'}
# The dwell row each half checks for the other half (DESIGN 7.6).
WATCH_ROW = {'actions': 'mac-job', 'mac': 'actions-run'}
# Alert kinds each half raises; plan_alerts counts only these as clean.
SCOPE = {
    'actions': ('source-failing', 'heartbeat-mac', 'canary', 'dwell'),
    'mac': ('heartbeat-actions',),
}
# DESIGN 7.2 checks with no local CLI (FRS-26). Part 6's age and dwell
# functions are called as well; a test asserts each one runs.
ACTIONS_CHECKS = ('check_frontmatter', 'check_models', 'check_paths', 'check_description', 'check_links')

TEXT_TIMEOUT = 30
TEXT_MAX_BYTES = 8 * 1024 * 1024
CLASSIFIER_TIMEOUT = 120
JWT_LIFETIME = 540

LOCAL_KEYS = ('slack_channel_id', 'prbot_path', 'path', 'held_sources', 'keychain_service', 'app_id',
              'installation_id')


class RunError(Exception):
    """A setup fault: bad arguments, config or credentials. Never holds a secret."""


# ---------------------------------------------------------------------------
# Tracked config: freshness/config.json


def load_config(path=CONFIG_PATH):
    with open(path, encoding='utf-8') as handle:
        config = json.load(handle)
    if not isinstance(config, dict) or config.get('version') != 1:
        raise RunError('freshness/config.json: version must be 1')
    if config.get('milestone') not in dwell.MILESTONES:
        raise RunError('freshness/config.json: unknown milestone')
    if config.get('advance_policy') not in age.POLICIES:
        raise RunError('freshness/config.json: unknown advance_policy')
    for key in ('link_min_gap_hours', 'issues_per_day', 'ops_messages_per_day', 'classifier_calls_per_day'):
        value = config.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RunError('freshness/config.json: %s must be a positive integer' % key)
    threshold = config.get('description_collision_threshold')
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 1:
        raise RunError('freshness/config.json: description_collision_threshold must be in (0, 1]')
    limits = config.get('dwell_limits')
    if not isinstance(limits, dict) or set(limits) != set(dwell.STATES):
        raise RunError('freshness/config.json: dwell_limits must have one entry per dwell state')
    for name, entry in limits.items():
        row = dwell.limit(name)
        unit = 'max_runs' if 'max_runs' in row else 'max_hours'
        value = (entry or {}).get(unit)
        if not isinstance(entry, dict) or set(entry) != {unit} or not isinstance(value, int) or value <= 0:
            raise RunError('freshness/config.json: dwell_limits.%s needs a positive %s' % (name, unit))
    return config


def apply_config(config):
    """Pass the tracked values to the modules that hold them as constants.

    The modules keep the same defaults; functions that take a parameter
    (the collision threshold, the link gap) get it at the call instead.
    Returns the previous values, for `restore_config`.
    """
    previous = {
        'issues': amod.ISSUES_PER_DAY, 'ops': amod.OPS_PER_DAY, 'classifier': cmod.CALLS_PER_DAY,
        'dwell': {row['state']: dict(row) for row in dwell.LIMITS},
    }
    amod.ISSUES_PER_DAY = config['issues_per_day']
    amod.OPS_PER_DAY = config['ops_messages_per_day']
    cmod.CALLS_PER_DAY = config['classifier_calls_per_day']
    for name, entry in config['dwell_limits'].items():
        dwell.limit(name).update(entry)
    return previous


def restore_config(previous):
    amod.ISSUES_PER_DAY = previous['issues']
    amod.OPS_PER_DAY = previous['ops']
    cmod.CALLS_PER_DAY = previous['classifier']
    for name, row in previous['dwell'].items():
        current = dwell.limit(name)
        current.clear()
        current.update(row)


# ---------------------------------------------------------------------------
# Local files outside the repository


def read_excluded(path=EXCLUDE_PATH):
    """Excluded skill folder names (DESIGN 5.1); empty when the list is absent (Actions)."""
    spec = importlib.util.spec_from_file_location('freshness_run_local_checks',
                                                  os.path.join(ROOT, 'tools', 'local_checks.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = module.read_names(os.path.expanduser(path))
    return set(names or ())


def load_local_config(path=LOCAL_CONFIG_PATH):
    """The Mac's untracked config, through part 7's loader (errors name keys only)."""
    config = notify.load_config(path)
    held = config.get('held_sources', {})
    if not isinstance(held, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in held.items()):
        raise RunError('freshness config: held_sources must map folder names to paths')
    for key in ('keychain_service', 'app_id', 'installation_id'):
        if not str(config.get(key) or '').strip():
            raise RunError('freshness config: missing key ' + key)
    return config


# ---------------------------------------------------------------------------
# Credentials


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def keychain_key(service, run=subprocess.run):
    """The App private key from the macOS Keychain; never written or printed."""
    try:
        proc = run(['security', 'find-generic-password', '-s', service, '-w'],
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RunError('cannot read the App key from the Keychain') from None
    key = proc.stdout or b''
    if isinstance(key, str):
        key = key.encode('utf-8')
    if proc.returncode != 0 or b'PRIVATE KEY' not in key:
        raise RunError('cannot read the App key from the Keychain')
    return key.strip() + b'\n'


def openssl_sign(key, data, run=subprocess.run):
    """RS256 signature of `data`: `openssl dgst -sha256 -sign`, key through a pipe."""
    read_fd, write_fd = os.pipe()
    try:
        # A PEM key is far below the pipe buffer, so this write never blocks.
        os.write(write_fd, key)
        os.close(write_fd)
        write_fd = None
        proc = run(['openssl', 'dgst', '-sha256', '-sign', '/dev/fd/%d' % read_fd],
                   input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   pass_fds=(read_fd,), timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RunError('openssl could not sign the App JWT') from None
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(read_fd)
    if proc.returncode != 0 or not proc.stdout:
        raise RunError('openssl could not sign the App JWT')
    return proc.stdout


def app_jwt(app_id, key, now, sign=openssl_sign):
    """A GitHub App JWT (RS256), valid from 60 s before `now` for 9 minutes."""
    issued = int(now.timestamp()) - 60
    header = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    payload = _b64url(json.dumps({'iat': issued, 'exp': issued + 60 + JWT_LIFETIME, 'iss': str(app_id)},
                                 separators=(',', ':')).encode())
    signing_input = (header + '.' + payload).encode('ascii')
    return header + '.' + payload + '.' + _b64url(sign(key, signing_input))


def _api_call(method, path, bearer, body=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header('Authorization', 'Bearer ' + bearer)
    request.add_header('Accept', 'application/vnd.github+json')
    request.add_header('X-GitHub-Api-Version', '2022-11-28')
    request.add_header('User-Agent', USER_AGENT)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode('utf-8') or 'null')
    except urllib.error.HTTPError as err:
        return err.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def mint_installation_token(config, now, key_reader=keychain_key, sign=openssl_sign, api=_api_call):
    """`(token, bot_login)` for the Mac half, from the App id and the Keychain key.

    The token is scoped to this repository with contents, issues and pull
    requests write (FRS-20). Errors never carry the key, the JWT or the token.
    """
    key = key_reader(config['keychain_service'])
    try:
        jwt = app_jwt(config['app_id'], key, now, sign=sign)
    finally:
        key = None
    status, app = api('GET', '/app', jwt)
    if status != 200 or not isinstance(app, dict) or not app.get('slug'):
        raise RunError('GitHub App lookup failed (status %s)' % status)
    body = {'repositories': [REPO.split('/', 1)[1]],
            'permissions': {'contents': 'write', 'issues': 'write', 'pull_requests': 'write'}}
    status, data = api('POST', '/app/installations/%s/access_tokens' % int(config['installation_id']), jwt, body)
    if status != 201 or not isinstance(data, dict) or not data.get('token'):
        raise RunError('installation token request failed (status %s)' % status)
    return data['token'], app['slug'] + '[bot]'


# ---------------------------------------------------------------------------
# Real network access (the defaults of the CLI entry point only)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_link(url, opener=None):
    """`(status, location)` for `links.check_links`: HEAD, GET on 405/501, no redirects."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    status, location = None, None
    for method in ('HEAD', 'GET'):
        request = urllib.request.Request(url, method=method, headers={'User-Agent': links.USER_AGENT})
        try:
            with opener.open(request, timeout=links.FETCH_TIMEOUT) as response:
                status, location = response.status, response.headers.get('Location')
        except urllib.error.HTTPError as err:
            status, location = err.code, err.headers.get('Location') if err.headers else None
        if method == 'HEAD' and status in links.FALLBACK_TO_GET:
            continue
        break
    return status, location


def make_fetch_text(token=None):
    """`fetch(url) -> (status, text)` for the upstream watch. The token goes only to api.github.com."""
    def fetch(url):
        headers = {'User-Agent': USER_AGENT}
        if token and urllib.parse.urlsplit(url).hostname == 'api.github.com':
            headers['Authorization'] = 'Bearer ' + token
            headers['Accept'] = 'application/vnd.github+json'
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TEXT_TIMEOUT) as response:
                return response.status, response.read(TEXT_MAX_BYTES).decode('utf-8', 'replace')
        except urllib.error.HTTPError as err:
            return err.code, ''
    return fetch


def fetch_npm(package):
    """`{"latest", "time"}` of an npm package, for `cli_checks.check_cli_lag`."""
    url = 'https://registry.npmjs.org/' + urllib.parse.quote(package, safe='@')
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=TEXT_TIMEOUT) as response:
        data = json.loads(response.read().decode('utf-8'))
    return {'latest': (data.get('dist-tags') or {}).get('latest'), 'time': data.get('time') or {}}


def anonymous_transport(method, path, body):
    """Read-only, unauthenticated GitHub transport for a dry run without a token."""
    if method != 'GET':
        raise ghmod.RefusedWrite('refused: dry run')
    request = urllib.request.Request(API + path, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    except (urllib.error.URLError, OSError) as err:
        raise ghmod.GitHubError('network error: ' + err.__class__.__name__) from None
    try:
        return status, (json.loads(raw.decode('utf-8')) if raw else None)
    except ValueError:
        return status, None


def read_only(transport):
    """Wrap a transport so no write request can be sent (dry run)."""
    def guarded(method, path, body):
        if method != 'GET':
            raise ghmod.RefusedWrite('refused: dry run writes nothing')
        return transport(method, path, body)
    return guarded


def make_classifier(path=None):
    """`call(prompt) -> reply` through `claude -p` on iwb's PATH."""
    runner = cli_checks.make_run(path, timeout=CLASSIFIER_TIMEOUT)

    def call(prompt):
        code, out = runner(['claude', '-p', prompt], CLASSIFIER_TIMEOUT)
        if code != 0:
            raise RuntimeError('claude -p exited %s' % code)
        return out
    return call


class Deps:
    """Everything a run touches outside the repository, injected for tests."""

    def __init__(self, github, clock, fetch_link=None, fetch_text=None, fetch_npm=None, cli_run=None,
                 classifier=None, notify_run=None, attention_fn=None, local_config=None, excluded=(),
                 private_names=(), out=None, run_id=None):
        self.github = github
        self.clock = clock
        self.fetch_link = fetch_link
        self.fetch_text = fetch_text
        self.fetch_npm = fetch_npm
        self.cli_run = cli_run
        self.classifier = classifier
        self.notify_run = notify_run
        self.attention_fn = attention_fn
        self.local_config = local_config or {}
        self.excluded = set(excluded or ())
        self.private_names = tuple(private_names or ())
        self.out = out if out is not None else sys.stdout
        self.run_id = run_id


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def default_deps(half, dry_run, environ=None, clock=_utcnow):
    """The real dependencies of one half. Raises RunError without credentials (non-dry runs)."""
    environ = os.environ if environ is None else environ
    now = clock()
    local = {}
    if half == 'mac':
        local = load_local_config()
    token = environ.get('FRESHNESS_TOKEN') or ''
    bot_login = environ.get('FRESHNESS_BOT_LOGIN') or local.get('bot_login') or None
    if half == 'mac' and not dry_run and not token:
        token, bot_login = mint_installation_token(local, now)
    if not token and not dry_run:
        raise RunError('FRESHNESS_TOKEN is not set; the freshness App is not configured (FRS-20)')
    transport = ghmod._urllib_transport(token) if token else anonymous_transport
    if dry_run:
        transport = read_only(transport)
    github = ghmod.GitHub(REPO, token, transport=transport, bot_login=bot_login)
    if half == 'actions':
        run_id = 'actions-%s' % (environ.get('GITHUB_RUN_ID') or now.strftime('%Y%m%dT%H%M%SZ'))
    else:
        run_id = 'mac-' + now.strftime('%Y%m%dT%H%M%SZ')
    path = local.get('path') or None
    return Deps(
        github=github, clock=clock, fetch_link=fetch_link, fetch_text=make_fetch_text(token),
        fetch_npm=fetch_npm, cli_run=cli_checks.make_run(path) if half == 'mac' else None,
        classifier=None if (dry_run or half != 'mac') else make_classifier(path),
        notify_run=subprocess.run, attention_fn=None, local_config=local, excluded=read_excluded(),
        run_id=run_id)


# ---------------------------------------------------------------------------
# The run


class Run:
    """One run of one half. `execute()` returns True when the run passed."""

    def __init__(self, half, deps, root=ROOT, config=None, dry_run=False):
        if half not in HALVES:
            raise RunError('--half must be actions or mac')
        self.half = half
        self.deps = deps
        self.root = os.path.abspath(root)
        self.config = config if config is not None else load_config()
        self.dry_run = dry_run
        self.gh = deps.github
        self.excluded = set(deps.excluded)
        self.findings = []
        self.alerts = []
        self.actions = []
        self.results = []
        self.errors = []
        self.failed = False
        self.calls = []     # names of the check functions called, for FRS-26 tests
        self.state = None
        self.items = []

    # -- helpers ---------------------------------------------------------

    def _print(self, text):
        self.deps.out.write(amod.scrub_text(text) + '\n')

    def _fail(self, step, err):
        self.failed = True
        self.errors.append('%s: %s' % (step, amod.scrub_text(str(err) or err.__class__.__name__)))

    def _step(self, name, fn, *args):
        try:
            return fn(*args)
        except ghmod.RefusedWrite:
            raise
        except Exception as err:  # one broken step must not stop the heartbeat
            self._fail(name, '%s: %s' % (err.__class__.__name__, err))
            return None

    def _skills(self):
        return checks.iter_skills(self.root, self.excluded)

    # -- state and GitHub items -----------------------------------------

    def load_state(self):
        text, _sha = self.gh.read_state()
        self.state = smod.load(text)
        for key in ('heartbeats', 'heartbeat_watch', 'results', 'pr_seen', 'upstream_pending', 'links', 'sources'):
            self.state.setdefault(key, {})

    def load_items(self):
        if not self.gh.bot_login:
            if not self.dry_run:
                raise RunError('the App bot login is not known (FRESHNESS_BOT_LOGIN)')
            self._print('dry run: no App bot login, so open freshness issues and PRs are not listed')
            self.items = []
            return
        self.items = self.gh.list_bot_items(self.now)

    def _open(self, prs=None):
        return [i for i in self.items if i.get('state') == 'open' and (prs is None or bool(i.get('is_pr')) == prs)]

    def _closed_issues(self):
        return [i for i in self.items if i.get('state') == 'closed' and not i.get('is_pr')]

    # -- checks: Actions half (FRS-26) ------------------------------------

    def actions_checks(self):
        models = checks.load_models()
        field_table = upstream.field_table(self.state['sources']) or None
        link_items = []
        per_skill = []
        for section, name, folder in self._skills():
            per_skill.append(name)
            self.calls.append('check_frontmatter')
            self.findings += checks.check_frontmatter(folder, name, field_table)
            self.calls.append('check_models')
            self.findings += checks.check_models(folder, name, models)
            self.calls.append('check_paths')
            self.findings += checks.check_paths(folder, name)
            for file, line, url in links.extract_links(folder):
                link_items.append((name, file, line, url))
        self.calls.append('check_description')
        self.findings += checks.check_description(checks.catalog_descriptions(self.root, self.excluded),
                                                  self.config['description_collision_threshold'])
        self.calls.append('check_links')
        found, history = links.check_links(link_items, self.state['links'], self.deps.fetch_link, self.now,
                                           self.config['link_min_gap_hours'])
        self.findings += found
        self.state['links'] = history
        self.checked_skills = per_skill

    def watch_upstream(self):
        self.calls.append('watch')
        sources = upstream.load_sources(os.path.join(self.root, upstream.DEFAULT_PATH))
        found, raised, self.state['sources'] = upstream.watch(
            sources, self.state['sources'], self.deps.fetch_text, self.now, self.deps.run_id)
        self.findings += found
        self.alerts += raised

    def age_checks(self):
        self.calls.append('age_findings')
        self.age_skills = age.read_skills(self.root, self.excluded)
        self.findings += age.age_findings(self.age_skills, self.now.date(), self.excluded)

    def verify_canary(self):
        self.calls.append('verify')
        ok, reason = canary.verify(self.findings, self.state['links'], now=self.now)
        self._print(reason)
        if not ok:
            self.failed = True
            self.errors.append('canary: ' + reason)
            self.alerts.append({
                'kind': 'canary', 'subject': canary.CANARY, 'evidence': 'Run %s: %s' % (self.deps.run_id, reason),
                'what_happened': 'The nightly checks did not report exactly the three seeded `zz-canary` '
                                 'findings, so the checks themselves may be broken (FRS-18, FRS-19).',
                'what_to_change': 'Start from `freshness/canary.py` and the check that lost or gained a finding.',
                'files': [canary.CANARY_FILE, 'freshness/canary.py'],
                'acceptance': amod.DEFAULT_ACCEPTANCE,
            })

    # -- checks: Mac half ---------------------------------------------------

    def mac_checks(self):
        cli_checks.reset_cache()
        run = cli_checks.cached(self.deps.cli_run)
        tools = upstream.tool_names(self.state['sources'])
        per_skill = []
        for section, name, folder in self._skills():
            per_skill.append(name)
            self.calls.append('check_commands')
            self.findings += cli_checks.check_commands(name, folder, run)
            self.calls.append('check_cli_refs')
            found = cli_checks.check_cli_refs(name, folder, tools, run)
            if not tools:  # no tools reference stored yet: only the flag checks count
                found = [f for f in found if f['kind'] != 'unknown-tool']
            self.findings += found
        self.calls.append('check_cli_lag')
        self.findings += cli_checks.check_cli_lag(run, self.deps.fetch_npm, self.now, self.state)
        self.calls.append('check_held')
        self.findings += held_drift.check_held(self.root, self.deps.local_config.get('held_sources') or {},
                                               self.excluded)
        self.checked_skills = per_skill

    # -- dwell and heartbeats (FRS-16, FRS-17) -----------------------------

    def watch_other_half(self):
        other = OTHER[self.half]
        beat = self.state['heartbeats'].get(other) or {}
        watch = self.state['heartbeat_watch']
        since = beat.get('last_success')
        if since:
            watch.pop(other, None)
        else:
            since = watch.setdefault(other, smod.iso(self.now))
        self.calls.append('check_dwell')
        self.alerts += dwell.check_dwell(WATCH_ROW[self.half], [{'id': other, 'since': since}], self.now,
                                         self.config['milestone'])

    def actions_dwell(self):
        seen = self.state['pr_seen']
        open_prs = []
        for item in self._open(prs=True):
            key = str(item['number'])
            seen.setdefault(key, smod.iso(self.now))
            open_prs.append({'id': item['number'], 'fingerprint': item['fingerprint'], 'since': seen[key]})
        for key in list(seen):
            if key not in {str(p['id']) for p in open_prs}:
                del seen[key]
        pings, items = dwell.ping_actions(open_prs, self.state, self.now)
        self.actions += pings
        self.alerts += dwell.check_dwell('freshness-pr-unreviewed', items, self.now, self.config['milestone'])
        self.rereview = age.rereview_since(self.state, self._open(prs=False), self.age_skills, self.now,
                                           self.excluded)
        self.alerts += dwell.check_dwell('re-review', [{'id': r['skill'], 'since': r['opened_at']}
                                                       for r in self.rereview],
                                         self.now, self.config['milestone'])

    # -- routing (FRS-12, FRS-21) ------------------------------------------

    def _skill_texts(self):
        out = {}
        for _section, name, folder in self._skills():
            try:
                with open(os.path.join(folder, 'SKILL.md'), encoding='utf-8') as handle:
                    out[name] = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
        return out

    def route_findings(self):
        models = checks.load_models()
        today = self.now.date()
        routed = []
        texts = None
        pending = self.state['upstream_pending']
        for found in self.findings:
            if found['kind'] == 'past-max-age':
                if found['skill'] not in self.excluded:
                    routed.append(age.route_rereview(found))
                continue
            if found['kind'] == 'upstream-change' and self.half == 'actions':
                item = rmod.route(found, self.root, models, today=today, excluded=self.excluded)
                if item is None or item['as'] == 'pr':
                    routed.append(item)
                    continue
                if texts is None:
                    texts = self._skill_texts()
                verdict = cmod.classify(found['evidence'], texts, self.state, None, today, self.excluded)
                if verdict is None:
                    # The rules found nothing: the Mac classifier decides (DESIGN 7.6).
                    pending[fmod.finding_fingerprint(found)] = found
                    continue
                routed.append(rmod.route(found, self.root, models, verdict, None, today, self.excluded))
                continue
            routed.append(rmod.route(found, self.root, models, today=today, excluded=self.excluded))
        if self.half == 'mac':
            routed += self.classify_pending(models, today)
        routed = [item for item in routed if item is not None]
        self.actions += amod.plan_findings(routed, self._open(), self.state, self.deps.private_names)

    def classify_pending(self, models, today):
        pending = self.state['upstream_pending']
        if not pending:
            return []
        texts = self._skill_texts()
        out = []
        for fp in sorted(pending):
            found = pending[fp]
            self.calls.append('classify')
            verdict = cmod.classify(found.get('evidence') or '', texts, self.state, self.deps.classifier, today,
                                    self.excluded)
            reason = None if verdict is not None else cmod.unclassified_reason(self.state, self.deps.classifier,
                                                                              today)
            out.append(rmod.route(found, self.root, models, verdict, reason, today, self.excluded))
            if not self.dry_run:
                del pending[fp]
        return out

    # -- age lifecycle (FRS-8, FRS-9) ----------------------------------------

    def record_results(self):
        """Per-skill pass/fail of this half, for `age.advance_actions`."""
        failing = {f['skill'] for f in self.findings if f['kind'] != 'past-max-age'}
        results = self.state['results']
        names = set(self.checked_skills)
        for name in list(results):
            if name not in names:
                del results[name]
        for name in self.checked_skills:
            entry = results.setdefault(name, {'actions_run': None, 'actions_ok': None, 'mac_run': None,
                                              'mac_ok': None, 'mac_at': None})
            ok = name not in failing and not self.failed
            if self.half == 'actions':
                entry.update(actions_run=self.deps.run_id, actions_ok=ok)
            else:
                entry.update(mac_run=self.deps.run_id, mac_ok=ok, mac_at=smod.iso(self.now))

    def age_actions(self):
        stale = age.read_stale(self.root)
        aged = [f['skill'] for f in self.findings if f['kind'] == 'past-max-age']
        self.calls.append('stale_actions')
        self.actions += age.stale_actions(self.rereview, stale, self.now, root=self.root, aged=aged,
                                          open_items=self._open(), excluded=self.excluded)
        self.calls.append('advance_actions')
        self.actions += age.advance_actions(self.state['results'], self.now.date(), self.config['advance_policy'],
                                            now=self.now, root=self.root, skills=self.age_skills,
                                            open_items=self._open(), excluded=self.excluded)

    # -- alerts, apply, announce --------------------------------------------

    def plan_alerts(self):
        planned, self.state = amod.plan_alerts(
            self.alerts, self._open(prs=False), self._closed_issues(), self.state, self.deps.run_id, self.now,
            scope=SCOPE[self.half], announce=(self.half == 'mac'))
        self.actions += planned

    def apply(self):
        if self.dry_run:
            for action in self.actions:
                shown = {'op': action.get('op'), 'fingerprint': (action.get('fingerprint') or '')[:12]}
                for key in ('number', 'title', 'branch', 'labels', 'text'):
                    if action.get(key) not in (None, ''):
                        shown[key] = action[key]
                if action.get('files'):
                    shown['files'] = sorted(action['files'])
                self._print('planned: ' + json.dumps(shown, sort_keys=True))
            self._print('planned actions: %d' % len(self.actions))
            return
        self.results = self.gh.apply(self.actions)
        for result in self.results:
            if not result.get('ok') and result.get('op') != 'send_ops':
                self._fail('apply', '%s %s' % (result.get('op'), result.get('error') or 'failed'))

    def announce(self):
        if self.dry_run:
            return
        open_alerts = [{'number': i['number'], 'fingerprint': i['fingerprint']}
                       for i in self._open(prs=False) if i['fingerprint'] in self.state.get('alerts', {})]
        for result in self.results:
            if result.get('op') in ('open_issue', 'reopen') and result.get('ok') \
                    and result.get('fingerprint') in self.state.get('alerts', {}):
                open_alerts.append({'number': result['number'], 'fingerprint': result['fingerprint'],
                                    'url': result.get('url')})
        self.calls.append('announce')
        self.state = notify.announce(open_alerts, self.state, self.deps.local_config, self.deps.notify_run,
                                     self.deps.attention_fn, self.now, ops_results=self.results)

    # -- heartbeat (FRS-16) -----------------------------------------------

    def counts(self):
        counts = {}
        for item in self.findings + self.alerts:
            counts[item['kind']] = counts.get(item['kind'], 0) + 1
        return dict(sorted(counts.items()))

    def heartbeat(self, end):
        previous = self.state['heartbeats'].get(self.half) or {}
        ok = not self.failed
        beat = {
            'run_id': self.deps.run_id,
            'start': smod.iso(self.now),
            'end': smod.iso(end),
            'result': 'ok' if ok else 'failed',
            'counts': self.counts(),
            'last_success': smod.iso(end) if ok else previous.get('last_success'),
        }
        self.state['heartbeats'][self.half] = beat
        return beat

    def write_state(self):
        # Keep the other half's newest heartbeat if it wrote while this run worked.
        other = OTHER[self.half]
        try:
            text, _sha = self.gh.read_state()
            fresh = smod.load(text).get('heartbeats', {}).get(other)
            if fresh:
                self.state['heartbeats'][other] = fresh
        except ghmod.GitHubError:
            pass
        self.gh.write_state(smod.dump(self.state),
                            'freshness: %s heartbeat, run %s (FRS-16)' % (self.half, self.deps.run_id))

    # -- the whole run ------------------------------------------------------

    def execute(self):
        self.now = smod.require_utc(self.deps.clock())
        self.checked_skills = []
        self.age_skills = []
        self.rereview = []
        self.load_state()
        self.load_items()
        if self.half == 'actions':
            self._step('upstream watch', self.watch_upstream)
            self._step('static checks', self.actions_checks)
            self._step('age', self.age_checks)
            self._step('canary', self.verify_canary)
            self._step('dwell', self.actions_dwell)
        else:
            self._step('cli checks', self.mac_checks)
        self._step('heartbeat watch', self.watch_other_half)
        self._step('route', self.route_findings)
        if self.half == 'actions':
            self._step('age actions', self.age_actions)
        self._step('alerts', self.plan_alerts)
        self.apply()
        if self.half == 'mac':
            self._step('announce', self.announce)
        self.record_results()
        beat = self.heartbeat(smod.require_utc(self.deps.clock()))
        if self.dry_run:
            self._print('dry run: heartbeat not written: ' + json.dumps(beat, sort_keys=True))
        else:
            try:
                self.write_state()
            except ghmod.GitHubError as err:
                self._fail('state write', err)
        for error in self.errors:
            self._print('error: ' + error)
        self._print('%s run %s: %s' % (self.half, self.deps.run_id, 'failed' if self.failed else 'ok'))
        return not self.failed


def parse_args(argv):
    parser = argparse.ArgumentParser(prog='python3 -B -m freshness.run', description=__doc__.split('\n\n')[0])
    parser.add_argument('--half', required=True, choices=HALVES)
    parser.add_argument('--dry-run', action='store_true', help='print the planned actions and write nothing')
    parser.add_argument('--root', default=ROOT, help='catalog checkout (default: this repository)')
    return parser.parse_args(argv)


def main(argv=None, deps=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    config = load_config()
    previous = apply_config(config)
    try:
        if deps is None:
            deps = default_deps(args.half, args.dry_run)
        ok = Run(args.half, deps, root=args.root, config=config, dry_run=args.dry_run).execute()
    except (RunError, notify.ConfigError, ghmod.GitHubError) as err:
        sys.stderr.write('freshness: %s\n' % amod.scrub_text(str(err)))
        return 1
    finally:
        restore_config(previous)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
