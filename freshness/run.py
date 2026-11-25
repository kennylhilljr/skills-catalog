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

Private repo list (FRS-29, FRS-30; DESIGN 7.6): the Actions half reads it
from the newline-separated `FRESHNESS_PRIVATE_REPOS` variable, the Mac half
from `~/.config/skills-catalog/private-repos.txt`. It stays in memory and
is never printed. A bot PR is opened only when the list is available and its
files pass the scrub against it; with no list (missing, empty or only
comments), every PR is planned as an issue without files, dry runs
included, and the run prints one fixed notice that names no repository.

State keys owned here: `heartbeats[half] = {"run_id", "start", "end",
"result", "counts", "last_success"}`, `heartbeat_watch[half]` (when this
half first saw the other with no success), `results[skill]` (per-skill
check results that `age.advance_actions` reads; each entry also carries
`actions_hash` and `mac_hash`, the content hash each half checked), `pr_seen[number]` (when a
freshness PR was first seen open, for the unreviewed-PR dwell row) and
`upstream_pending[fp]` (upstream findings waiting for the Mac classifier).

Every other module takes its network, GitHub and command access as
parameters; the real ones are built here, in `default_deps`, and nowhere
else. Python 3.9+, standard library only. Importing has no side effects.
"""

import argparse
import base64
import copy
import datetime
import functools
import importlib.util
import json
import os
import subprocess
import sys
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


PRIVATE_ENV = 'FRESHNESS_PRIVATE_REPOS'
# One fixed notice per half when its private repo list is unavailable (FRS-30).
# It never holds a name, the list or any file text.
PRIVATE_LIST_NOTICE = {
    'actions': '::notice::FRESHNESS_PRIVATE_REPOS is not set or lists no names, so bot PRs are '
               'planned as issues without their files (FRS-30).',
    'mac': 'notice: ~/.config/skills-catalog/private-repos.txt is missing or lists no names, so bot PRs '
           'are planned as issues without their files (FRS-30).',
}


def parse_private_names(text):
    """Names from private repo list text, parsed like `tools/scrub.read_private_list`."""
    names = []
    for raw in (text or '').splitlines():
        name = raw.split('#', 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def private_names_from_env(environ):
    """The Actions half's private repo list: newline-separated names in FRESHNESS_PRIVATE_REPOS.

    Read only from `environ` and kept in memory; empty when the variable is
    missing, empty or holds only comments, which makes the run fail closed.
    """
    return parse_private_names(environ.get(PRIVATE_ENV) or '')


def read_private_names(path=None):
    """The Mac half's private repo list the scrub checks bot PR files against; empty when absent."""
    amod._scan('')  # loads tools/scrub.py once
    scrub = amod._scrub_module
    path = os.path.expanduser(path or scrub.DEFAULT_PRIVATE_LIST)
    if not os.path.exists(path):
        return ()
    try:
        return tuple(scrub.read_private_list(path))
    except (OSError, UnicodeDecodeError):
        raise RunError('the private repo list cannot be read')


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


def _unhex_keychain(value):
    """`security -w` prints a password holding a newline (every PEM key) as hex; decode it."""
    text = value.strip()
    if text and len(text) % 2 == 0 and all(c in b'0123456789abcdefABCDEF' for c in text):
        try:
            return bytes.fromhex(text.decode('ascii'))
        except ValueError:
            return value
    return value


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
    key = _unhex_keychain(key)
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


def runner_env(path=None):
    """The environment of the Mac half's subprocesses: iwb's PATH, `~/.local/bin` first.

    launchd starts the job with a minimal PATH, so `openclaw`, `claude` and
    `codex` are found through the configured `path`, as in
    `cli_checks.make_run`.
    """
    local_bin = os.path.expanduser(os.path.join('~', '.local', 'bin'))
    parts = [p for p in (path if path is not None else os.environ.get('PATH', '')).split(os.pathsep) if p]
    env = dict(os.environ)
    env['PATH'] = os.pathsep.join([local_bin] + [p for p in parts if p != local_bin])
    return env


def make_notify_run(path=None):
    """`subprocess.run` for part 7's `send`, with the configured PATH."""
    return functools.partial(subprocess.run, env=runner_env(path))


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
                 private_names=None, out=None, run_id=None):
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
        self.private_names = tuple(n for n in (private_names or ()) if str(n or '').strip())
        # No names (omitted, missing, empty or comment-only) means the list is
        # unavailable: every bot PR is planned as an issue (fail closed, FRS-30).
        self.private_list_ok = bool(self.private_names)
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
        notify_run=make_notify_run(path), attention_fn=None, local_config=local, excluded=read_excluded(),
        private_names=private_names_from_env(environ) if half == 'actions' else read_private_names(),
        run_id=run_id)


# ---------------------------------------------------------------------------
# State merge

_MISSING = object()


def merge_state(base, ours, fresh):
    """Three-way merge of the state document, for halves that overlap.

    `base` is the state this run read, `ours` what it wants to write and
    `fresh` what is on `freshness-state` now. A value only one side changed
    keeps that change; where both changed, dicts merge key by key, a change
    beats a delete, and any other value keeps this run's. So the other half's heartbeat, results
    fields, link or source history and alert entries survive this write.
    """
    if ours == base:
        return fresh if fresh is _MISSING else copy.deepcopy(fresh)
    if fresh is _MISSING or fresh == base or fresh == ours:
        return ours if ours is _MISSING else copy.deepcopy(ours)
    if ours is _MISSING:
        # This run deleted it and the other half changed it: the change wins,
        # so a newer entry (say an upstream finding parked again) is not lost.
        return copy.deepcopy(fresh)
    if not (isinstance(base, dict) and isinstance(ours, dict) and isinstance(fresh, dict)):
        if isinstance(ours, dict) and isinstance(fresh, dict):
            base = {}
        else:
            return copy.deepcopy(ours)
    out = {}
    for key in sorted(set(base) | set(ours) | set(fresh), key=str):
        value = merge_state(base.get(key, _MISSING), ours.get(key, _MISSING), fresh.get(key, _MISSING))
        if value is not _MISSING:
            out[key] = value
    return out


def keep_concurrent_announcements(base, merged, fresh):
    """Keep a delivery the other half made for an alert this run just created.

    Both halves can open the same alert entry from a base that lacked it. The
    generic merge then lets this run's `announced: false` beat the other
    half's `true`, and the next Mac run would announce the open issue again.
    """
    base_alerts = (base or {}).get('alerts') or {}
    fresh_alerts = (fresh or {}).get('alerts') or {}
    for fp, entry in (merged.get('alerts') or {}).items():
        theirs = fresh_alerts.get(fp)
        if fp in base_alerts or not (isinstance(entry, dict) and isinstance(theirs, dict)):
            continue
        if theirs.get('announced') and not entry.get('announced'):
            entry['announced'] = True
            entry['last_reminder_at'] = theirs.get('last_reminder_at')
    return merged


STATE_WRITE_ATTEMPTS = 3


def cas_write_state(gh, text, parent, message):
    """One compare-and-swap write of the state file; False when the branch moved.

    `parent` is the `freshness-state` sha the text was merged against (None
    before the first write, which creates the branch). Unlike
    `GitHub.write_state`, it never retries with the same text, so the caller
    can merge first. Every request still goes through part 5's guarded
    writer (INV-5): only `refs/heads/freshness-state` can move.
    """
    if parent is None:
        sha = gh._commit_files({ghmod.STATE_PATH: text}, message, None, None)
        gh._check_ref(ghmod.STATE_REF)
        status, _ = gh._request('POST', '/git/refs', {'ref': ghmod.STATE_REF, 'sha': sha}, ok=(201, 422))
        return status == 201
    _, commit = gh._request('GET', '/git/commits/' + parent)
    sha = gh._commit_files({ghmod.STATE_PATH: text}, message, parent, commit['tree']['sha'])
    status, _ = gh._request('PATCH', '/git/' + ghmod.STATE_REF, {'sha': sha, 'force': False}, ok=(200, 409, 422))
    return status == 200


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
        except Exception as err:  # one broken step, even a refused write, must not stop the heartbeat
            self._fail(name, '%s: %s' % (err.__class__.__name__, err))
            return None

    def _skills(self):
        return checks.iter_skills(self.root, self.excluded)

    def _checked_skills(self):
        """`_skills()`, refused when one name is under both `skills/` and `.held/` (FRS-9).

        Hashes and results are keyed by name alone, so a duplicate would let
        one folder's hash stand for the other's content.
        """
        skills = self._skills()
        seen = {}
        dupes = set()
        for section, name, _folder in skills:
            if name in seen and seen[name] != section:
                dupes.add(name)
            seen.setdefault(name, section)
        if dupes:
            raise RunError('skill names in more than one section: ' + ', '.join(sorted(dupes)))
        return skills

    def _settle_hashes(self, folders, hashes):
        """Hash each folder again after every check ran (FRS-9).

        The checks reopen the live files, so a folder that changed while they
        ran may have been checked as a mix of old and new bytes. Such a skill
        records no hash and fails, and so does the whole half, so it cannot
        pair with the other half on content this half never checked.
        """
        self.changed_skills = set()
        for name, folder in folders.items():
            if age.content_hash(folder) != hashes[name]:
                hashes[name] = None
                self.changed_skills.add(name)
        if self.changed_skills:
            self._fail('content hash', 'changed while the checks read them: '
                       + ', '.join(sorted(self.changed_skills)))

    def settle_actions_hashes(self):
        """Run after the age check too, since `age.read_skills` reopens each SKILL.md (FRS-9)."""
        self._settle_hashes(self.hash_folders, self.checked_hashes)

    # -- state and GitHub items -----------------------------------------

    def load_state(self):
        text, self.state_sha = self.gh.read_state()
        self.state = smod.load(text)
        for key in ('heartbeats', 'heartbeat_watch', 'results', 'pr_seen', 'upstream_pending', 'links', 'sources'):
            self.state.setdefault(key, {})
        self.base_state = copy.deepcopy(self.state)

    def load_items(self):
        if not self.gh.bot_login:
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
        hashes = {}
        folders = {}
        for section, name, folder in self._checked_skills():
            per_skill.append(name)
            folders[name] = folder
            hashes[name] = age.content_hash(folder)  # before the checks; compared again after them
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
        # Compared again in `settle_actions_hashes`, after the age check also read the files.
        self.hash_folders = folders
        self.checked_skills = per_skill
        self.checked_hashes = hashes

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
        hashes = {}
        folders = {}
        for section, name, folder in self._checked_skills():
            per_skill.append(name)
            folders[name] = folder
            hashes[name] = age.content_hash(folder)  # before the checks; compared again after them
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
        self._settle_hashes(folders, hashes)
        self.checked_skills = per_skill
        self.checked_hashes = hashes

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
        if not self.deps.private_list_ok:
            for item in routed:
                if item.get('as') == 'pr':
                    item.update({'as': 'issue', 'files': {}})
        self.actions += amod.plan_findings(routed, self._open(), self.state, self.deps.private_names)

    # -- publication policy for bot PRs (FRS-29, FRS-30) ------------------

    def guard_prs(self, actions):
        """`actions` with every `open_pr` the publication checks reject planned as an issue.

        A PR is published only when the private repo list is available and
        its files pass the scrub against it. Otherwise it becomes an
        `open_issue` without files or branch, and none at all when an open
        item already has its fingerprint.
        """
        open_fps = {i.get('fingerprint') for i in self._open() if i.get('fingerprint')}
        out = []
        for action in actions:
            if action.get('op') != 'open_pr':
                out.append(action)
                continue
            if action.get('fingerprint') in open_fps:
                continue  # an open item (say an earlier fallback issue) already covers it (FRS-13)
            if self.deps.private_list_ok and not any(
                    amod._scan_file(path, text, self.deps.private_names)
                    for path, text in (action.get('files') or {}).items()):
                out.append(action)
                continue
            issue = {k: v for k, v in action.items() if k not in ('files', 'branch')}
            issue['op'] = 'open_issue'
            out.append(issue)
        return out

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

    def record_results(self, results=None):
        """Per-skill pass/fail of this half into `results` (default: the state), for `age.advance_actions`.

        `execute` records into the state only after routing, alert planning
        and apply ran, so a step that fails late still leaves `*_ok: false`.
        `age_actions` reads a copy recorded the same way before then.
        Each half also records the content hash (`age.content_hash`) of the
        folder it checked, and `age.eligible` pairs the halves only when the
        hashes match.
        """
        if results is None:
            results = self.state['results']
        if not self.checked_skills:
            # The checks did not finish: no skill counts as passing for this half.
            prefix = 'actions_' if self.half == 'actions' else 'mac_'
            for entry in results.values():
                entry[prefix + 'ok'] = False
            return
        failing = {f['skill'] for f in self.findings if f['kind'] != 'past-max-age'}
        names = set(self.checked_skills)
        hashes = getattr(self, 'checked_hashes', {})
        for name in list(results):
            if name not in names:
                del results[name]
        for name in self.checked_skills:
            entry = results.setdefault(name, {})  # each half sets only its own fields, so merges keep both
            ok = name not in failing and name not in getattr(self, 'changed_skills', ()) and not self.failed
            if self.half == 'actions':
                entry.update(actions_run=self.deps.run_id, actions_ok=ok, actions_hash=hashes.get(name))
            else:
                entry.update(mac_run=self.deps.run_id, mac_ok=ok, mac_at=smod.iso(self.now),
                             mac_hash=hashes.get(name))

    def age_actions(self):
        stale = age.read_stale(self.root)
        aged = [f['skill'] for f in self.findings if f['kind'] == 'past-max-age']
        self.calls.append('stale_actions')
        self.actions += self.guard_prs(age.stale_actions(self.rereview, stale, self.now, root=self.root,
                                                         aged=aged, open_items=self._open(),
                                                         excluded=self.excluded))
        self.calls.append('advance_actions')
        results = copy.deepcopy(self.state['results'])
        self.record_results(results)  # this run's results, not yet in the state
        # Applied only after every other action landed (FRS-9: a failed run advances nothing).
        self.advance = self.guard_prs(age.advance_actions(
            results, self.now.date(), self.config['advance_policy'], now=self.now, root=self.root,
            skills=self.age_skills, open_items=self._open(), excluded=self.excluded))

    # -- alerts, apply, announce --------------------------------------------

    def plan_alerts(self):
        planned, self.state = amod.plan_alerts(
            self.alerts, self._open(prs=False), self._closed_issues(), self.state, self.deps.run_id, self.now,
            scope=SCOPE[self.half], announce=(self.half == 'mac'))
        self.actions += planned

    def _plan_alerts_ok(self):
        self.plan_alerts()
        return True

    def apply(self):
        if self.skip_writes:
            return
        if self.dry_run:
            for action in self.actions + self.advance:
                shown = {'op': action.get('op'), 'fingerprint': (action.get('fingerprint') or '')[:12]}
                for key in ('number', 'title', 'branch', 'labels', 'text'):
                    if action.get(key) not in (None, ''):
                        shown[key] = action[key]
                if action.get('files'):
                    shown['files'] = sorted(action['files'])
                self._print('planned: ' + json.dumps(shown, sort_keys=True))
            self._print('planned actions: %d' % len(self.actions + self.advance))
            return
        self.results = self._apply(self.actions)
        if self.advance and not self.failed:
            self.results += self._apply(self.advance)

    def _apply(self, actions):
        results = self.gh.apply(actions)
        for result in results:
            if not result.get('ok') and result.get('op') != 'send_ops':
                self._fail('apply', '%s %s' % (result.get('op'), result.get('error') or 'failed'))
        self._acknowledge_evidence(actions, results)
        return results

    def _acknowledge_evidence(self, actions, results):
        """Record a finding's new evidence hash once its comment landed (FRS-13).

        `GitHub.apply` returns one result per action, in order. Only a
        comment that carries `finding_evidence_sha` and whose result is a
        successful comment for the same fingerprint moves the hash, so a
        failed or missing comment is planned again on the next run.
        """
        table = self.state.setdefault('findings', {})
        for action, result in zip(actions, results):
            sha = action.get('finding_evidence_sha') if isinstance(action, dict) else None
            if not sha or action.get('op') != 'comment':
                continue
            fp = action.get('fingerprint')
            if result.get('op') != 'comment' or result.get('fingerprint') != fp or result.get('ok') is not True:
                continue
            record = table.get(fp)
            if isinstance(record, dict):
                record['evidence_sha'] = sha
            else:
                table[fp] = {'evidence_sha': sha}

    def keep_upstream_edges(self):
        """Undo this run's upstream bookkeeping when its findings were not delivered.

        The upstream watch is edge-triggered: the new source hashes and the
        Mac's cleared `upstream_pending` entries would otherwise consume a
        change whose issue or PR was never opened. The next run sees it again;
        open items dedupe it if part of it did land.
        """
        base = self.base_state['sources']
        for sid, entry in self.state['sources'].items():
            old = base.get(sid)
            if not (isinstance(entry, dict) and isinstance(old, dict) and old.get('sha256')):
                continue  # a first baseline raised nothing to deliver
            # Only the content baseline goes back; fetch-failure counters keep
            # counting toward `source-failing` (FRS-14).
            for key in ('sha256', 'extract', 'fetched_at'):
                entry[key] = copy.deepcopy(old.get(key))
        pending = self.state['upstream_pending']
        for fp, found in self.base_state['upstream_pending'].items():
            pending.setdefault(fp, copy.deepcopy(found))

    def announce(self):
        if self.dry_run:
            return
        # FRS-23: an alert issue whose state write failed on the Actions side is still announced.
        open_alerts = [{'number': i['number'], 'fingerprint': i['fingerprint'], 'title': i.get('title')}
                       for i in self._open(prs=False)
                       if i['fingerprint'] in self.state.get('alerts', {})
                       or amod.ALERT_LABEL in (i.get('labels') or ())]
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
        """Commit the state with compare-and-swap; on a conflict, merge the other half's write and retry.

        Each attempt commits on the sha it merged against and moves the ref
        only as a fast-forward, so a commit the other half made in between
        is never overwritten: the move fails and the next attempt merges it.
        """
        message = 'freshness: %s heartbeat, run %s (FRS-16)' % (self.half, self.deps.run_id)
        for _attempt in range(STATE_WRITE_ATTEMPTS):
            text, sha = self.gh.read_state()
            if sha != self.state_sha:
                fresh = smod.load(text)
                self.state = keep_concurrent_announcements(
                    self.base_state, merge_state(self.base_state, self.state, fresh), fresh)
                self.base_state, self.state_sha = fresh, sha
            if cas_write_state(self.gh, smod.dump(self.state), sha, message):
                return
        raise ghmod.GitHubError('state write conflicted %d times' % STATE_WRITE_ATTEMPTS)

    # -- the whole run ------------------------------------------------------

    def execute(self):
        self.now = smod.require_utc(self.deps.clock())
        self.checked_skills = []
        self.checked_hashes = {}
        self.hash_folders = {}
        self.changed_skills = set()
        self.age_skills = []
        self.rereview = []
        self.skip_writes = False
        self.advance = []
        if not self.deps.private_list_ok:
            # Once per run, PR or not, so a missing list is visible (FRS-30).
            self._print(PRIVATE_LIST_NOTICE[self.half])
        # No lease: the halves run on separate schedules and every state write
        # is a compare-and-swap three-way merge (FRS-16). A rare overlap can
        # plan against the same daily caps; that is accepted (issue #72).
        self.load_state()
        if not self.gh.bot_login and not self.dry_run:
            raise RunError('the App bot login is not known (FRESHNESS_BOT_LOGIN)')
        self._step('list freshness items', self.load_items)
        if self.failed:
            # Without the open items every finding would look new: plan nothing.
            self.skip_writes = True
        if self.half == 'actions':
            self._step('upstream watch', self.watch_upstream)
            self._step('static checks', self.actions_checks)
            self._step('age', self.age_checks)
            self._step('content hash', self.settle_actions_hashes)
            self._step('canary', self.verify_canary)
            if not self.skip_writes:
                self._step('dwell', self.actions_dwell)
        else:
            self._step('cli checks', self.mac_checks)
        self._step('heartbeat watch', self.watch_other_half)
        before = len(self.errors)
        if not self.skip_writes:
            self._step('route', self.route_findings)
            if self.half == 'actions' and not self.failed:
                # FRS-9: a run that failed (a broken check, the canary) advances nothing.
                self._step('age actions', self.age_actions)
            planned = self._step('alerts', self._plan_alerts_ok)
            if planned:
                self._step('apply', self.apply)
                if self.half == 'mac':
                    self._step('announce', self.announce)
            else:
                self._fail('apply', 'skipped: alert planning failed, so the planned actions were not applied')
        undelivered = self.skip_writes or any(
            e.split(':', 1)[0] in ('route', 'age actions', 'alerts', 'apply') for e in self.errors[before:])
        if undelivered and not self.dry_run:
            self.keep_upstream_edges()
        # Recorded last, so any failure above leaves this half's results failing.
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
