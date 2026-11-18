"""The Mac ops notifier: OpenClaw attention plus one Slack message per alert.

Requirement: FRS-23, under the caps and retries of FRS-24 and FRS-25
(docs/SPEC.md). Contract: docs/DESIGN.md 7.8 and 7.6.

Only the launchd half on the Mac runs this; the Actions half cannot reach
OpenClaw and only opens issues. For each alert the Mac job

1. calls `prbot.openclaw_attention("freshness-alert: <kind>", {url, title,
   problem})`, loading prbot from the local `prbot_path` the way iwb does, so
   the alert sits next to iwb's items in OpenClaw;
2. runs `openclaw message send --channel slack --target channel:<id> -m
   <text>` to `#github-feed`, where Kenny's reply starts an ops turn.

`announce` delivers part 1's `send_ops` results (as returned by
`freshness.github.GitHub.apply`, with `{issue_url}` filled in) plus one
message for every open alert issue not yet announced, including issues the
Actions half opened while the Mac was down. All of them share part 1's daily
cap of 6 messages and one digest, and `alerts.record_send` is called after
each send, so a failed send is retried on the next run.

The Slack channel id comes from the untracked local config
`~/.config/skills-catalog/freshness.json` and is never printed, logged,
written to the state or put in an exception message.

Command runners and prbot are injected; nothing here touches the network,
`openclaw` or GitHub unless the caller passes the real ones (part 9).
Python 3.9+, standard library only. Importing has no side effects.
"""

import copy
import importlib.machinery
import importlib.util
import json
import os
import sys

from . import alerts as amod
from . import finding as fmod
from . import state as smod

CONFIG_PATH = '~/.config/skills-catalog/freshness.json'
REQUIRED_KEYS = ('slack_channel_id', 'prbot_path')
DEFAULT_OPENCLAW_BIN = 'openclaw'
SEND_TIMEOUT = 30
MESSAGE_MAX = 299  # "under 300 characters"
ATTENTION_PREFIX = 'freshness-alert: '
ANNOUNCE_PREFIX = 'freshness-alert'
_TITLE_PREFIX = 'freshness-alert: '
_ANNOUNCE_HEADS = ('skills-catalog freshness-alert: ', 'skills-catalog freshness-alert reopened: ')


class ConfigError(ValueError):
    """A problem with the local freshness config; the message never holds its content."""


def load_config(path=CONFIG_PATH):
    """Read the local config: `{"slack_channel_id", "prbot_path", "openclaw_bin"}`.

    `openclaw_bin` defaults to `openclaw`. Other keys (used by part 9) are
    kept. A missing or empty key is a ConfigError naming the key only; a
    file that cannot be read or parsed gives an error naming neither its
    content nor its full path.
    """
    full = os.path.expanduser(path)
    name = os.path.basename(full)
    try:
        with open(full, encoding='utf-8') as handle:
            text = handle.read()
    except OSError:
        raise ConfigError('freshness config: cannot read ' + name) from None
    try:
        config = json.loads(text)
    except ValueError:
        raise ConfigError('freshness config: ' + name + ' is not valid JSON') from None
    if not isinstance(config, dict):
        raise ConfigError('freshness config: ' + name + ' must hold a JSON object')
    for key in REQUIRED_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError('freshness config: missing key ' + key)
    bin_ = config.get('openclaw_bin', DEFAULT_OPENCLAW_BIN)
    if not isinstance(bin_, str) or not bin_.strip():
        raise ConfigError('freshness config: missing key openclaw_bin')
    config['openclaw_bin'] = bin_
    return config


def _load_prbot(prbot_path):
    """Load prbot the way iwb does; `prbot_path` is the `prbot` file or its folder."""
    path = os.path.expanduser(prbot_path)
    if os.path.isdir(path):
        folder = path
        candidates = [os.path.join(path, 'prbot'), os.path.join(path, 'prbot.py')]
        source = next((c for c in candidates if os.path.isfile(c)), None)
    else:
        folder = os.path.dirname(path) or '.'
        source = path if os.path.isfile(path) else None
    if source is None:
        raise ImportError('prbot not found')
    inserted = folder not in sys.path
    if inserted:
        sys.path.insert(0, folder)
    try:
        loader = importlib.machinery.SourceFileLoader('prbot', source)
        spec = importlib.util.spec_from_loader('prbot', loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        if inserted:
            try:
                sys.path.remove(folder)
            except ValueError:
                pass


def attention(kind, url, title, problem, prbot_path):
    """Call `prbot.openclaw_attention`; returns its result, or an error text.

    An import or call error is returned as a string, never raised, so the
    Slack message still goes out.
    """
    try:
        prbot = _load_prbot(prbot_path)
        return prbot.openclaw_attention(ATTENTION_PREFIX + kind, {'url': url, 'title': title, 'problem': problem})
    except Exception as err:  # prbot or OpenClaw being absent must not stop the message
        return 'attention failed: ' + err.__class__.__name__


def send(text, config, run):
    """Send one ops message to `#github-feed`; True when `openclaw` exits 0.

    `run` has the `subprocess.run` signature. No shell, 30-second timeout.
    Output is captured and dropped, so the channel id is never printed.
    """
    argv = [config.get('openclaw_bin') or DEFAULT_OPENCLAW_BIN, 'message', 'send', '--channel', 'slack',
            '--target', 'channel:' + config['slack_channel_id'], '-m', text]
    try:
        result = run(argv, timeout=SEND_TIMEOUT, capture_output=True, text=True, check=False)
    except Exception:  # timeout, missing binary: recorded as a failed send
        return False
    return getattr(result, 'returncode', 1) == 0


def _fit(text):
    """`text` under 300 characters; a digest drops lines, a message shortens its subject."""
    if len(text) <= MESSAGE_MAX:
        return text
    if '\n' in text:
        lines = text.split('\n')
        kept = [lines[0][:MESSAGE_MAX - 20]]
        rest = lines[1:]
        while rest:
            tail = '\n- and ' + str(len(rest) - 1) + ' more'
            if len('\n'.join(kept + [rest[0]])) + len(tail) > MESSAGE_MAX:
                break
            kept.append(rest.pop(0))
        if rest:
            kept.append('- and ' + str(len(rest)) + ' more')
        return '\n'.join(kept)
    head, sep, url = text.rpartition(': ')
    if sep and url.startswith('http') and len(url) < MESSAGE_MAX - 40:
        return head[:MESSAGE_MAX - len(url) - 5] + '...: ' + url
    return text[:MESSAGE_MAX - 3] + '...'


def _clean(text):
    """Plain, scrubbed, under 300 characters."""
    return _fit(amod.scrub_text(text))


def _kind_subject(issue, entry):
    if entry and entry.get('kind'):
        return entry['kind'], entry.get('subject') or ''
    title = issue.get('title') or ''
    if title.startswith(_TITLE_PREFIX):
        kind, _, subject = title[len(_TITLE_PREFIX):].partition(': ')
        if kind:
            return kind, subject
    return 'unknown', '#' + str(issue.get('number'))


def _issue_url(issue):
    return issue.get('url') or issue.get('html_url') or amod.ISSUE_URL_BASE + str(issue.get('number'))


def _digest(pending):
    return (amod.DIGEST_PREFIX + 'daily cap of ' + str(amod.OPS_PER_DAY) + ' ops messages reached, '
            + str(len(pending)) + ' more:\n' + '\n'.join('- ' + t for t in pending))


def _new_entry(kind, subject, number):
    """A part 1 alerts entry (freshness/state.py) for an issue the state does not know."""
    return {'kind': kind, 'subject': amod.scrub_text(subject), 'issue': number, 'evidence_sha': '',
            'last_comment_at': None, 'last_reminder_at': None, 'announced': False,
            'clean_runs': [], 'closed_at': None}


def announce(open_alert_issues, state, config, run, attention_fn, now, ops_results=()):
    """Announce open alerts and deliver the planned ops messages (FRS-23, FRS-25).

    `open_alert_issues`: open `freshness-alert` issues, `{"number",
    "fingerprint", "title"?, "url"?}` parsed from the fingerprint marker.
    `ops_results`: the results of part 5's `apply` for part 1's actions.
    Its `send_ops` items (`{"op": "send_ops", "fingerprint", "text", "ok"}`)
    are delivered; one with `ok` False or an unfilled `{issue_url}` is
    recorded as failed without sending. Its `open_issue` items give the URLs
    of issues opened this run.
    `attention_fn(kind, url, title, problem)` is called once per alert
    announcement that is sent or folded into the digest; its errors are
    ignored. None means `attention` with the config's `prbot_path`. `run`
    is passed to `send`.

    Returns the new state; the given state is not changed. An alert is
    marked `announced` only after its message (or the digest holding it)
    was sent; a failed send is recorded with `alerts.record_send` and is
    due again on the next run.
    """
    now = smod.require_utc(now)
    now_iso = smod.iso(now)
    new = copy.deepcopy(state) if state is not None else smod.empty_state()
    smod.roll_day(new, now)
    table = new.setdefault('alerts', {})
    ops = new['ops']

    results = [r for r in ops_results or () if isinstance(r, dict)]
    planned = [r for r in results if r.get('op', 'send_ops') == 'send_ops']
    opened_urls = {r.get('fingerprint'): r['url'] for r in results
                   if r.get('op') == 'open_issue' and r.get('ok') and r.get('url')}
    open_urls = {i.get('fingerprint'): _issue_url(i) for i in open_alert_issues or () if i.get('fingerprint')}
    planned_fps = set(r.get('fingerprint') for r in planned)
    attended = set()
    if attention_fn is None:
        def attention_fn(kind, url, title, problem):
            return attention(kind, url, title, problem, config['prbot_path'])

    def call_attention(kind, url, title, problem):
        try:
            attention_fn(kind, url, title, amod.scrub_text(problem))
        except Exception:  # attention is best effort (DESIGN 7.8)
            pass

    # Open alert issues not yet announced, decided before any send so an
    # alert whose planned message fails below is not sent twice this run.
    mine = []  # (fp, text, attention args)
    for issue in sorted(open_alert_issues or (), key=lambda i: (i.get('number') or 0)):
        fp = issue.get('fingerprint')
        if not fp or fp in planned_fps or fp in (m[0] for m in mine):
            continue
        entry = table.get(fp)
        if entry is None:
            kind, subject = _kind_subject(issue, None)
            entry = table[fp] = _new_entry(kind, subject, issue.get('number'))
        elif entry.get('closed_at'):
            entry['closed_at'] = None  # reopened outside the planner
            entry['announced'] = False
        if entry.get('announced'):
            continue
        entry['issue'] = issue.get('number')
        kind, subject = _kind_subject(issue, entry)
        url = _issue_url(issue)
        title = amod.scrub_text(issue.get('title') or amod.alert_title(kind, subject))
        problem = 'freshness alert ' + kind + ' is open for ' + (subject or 'the catalog')
        mine.append((fp, amod._ops_head(ANNOUNCE_PREFIX, entry) + ': ' + url, (kind, url, title, problem)))

    # 1. Part 1's planned messages: their cap slots are already counted.
    incoming_digest = False
    for result in planned:
        text = result.get('text') or ''
        fp = result.get('fingerprint')
        if text.startswith(amod.DIGEST_PREFIX):
            incoming_digest = True  # rebuilt below with this function's lines
            continue
        if not result.get('ok', True) or amod.ISSUE_URL_PLACEHOLDER in text or not text:
            amod.record_send(new, text, False, fp)
            continue
        entry = table.get(fp)
        if entry is not None and fp not in attended and text.startswith(_ANNOUNCE_HEADS):
            url = text.rpartition(': ')[2]
            call_attention(entry['kind'], url, amod.alert_title(entry['kind'], entry['subject']),
                           'freshness alert ' + entry['kind'] + ' is open for ' + entry['subject'])
            attended.add(fp)
        ok = send(_clean(text), config, run)
        amod.record_send(new, text, ok, fp)

    # 2. This function's announcements, under the same daily cap.
    folded = []  # fps whose line went into the digest
    for fp, text, args in mine:
        text = amod.scrub_text(text)
        if ops['sent'] < amod.OPS_PER_DAY:
            call_attention(*args)
            ops['sent'] += 1
            ok = send(_clean(text), config, run)
            amod.record_send(new, text, ok, fp)
            if ok:
                table[fp]['announced'] = True
                table[fp]['last_reminder_at'] = now_iso
        elif not ops['digest_sent'] or incoming_digest:
            call_attention(*args)
            ops['pending'].append(text)
            folded.append(fp)
        # After the digest, nothing more is sent today; the alert stays due.

    # 3. One digest for everything past the cap. The planner's lines in it
    # are announcements it folded this run (the digest goes out once a day),
    # so their alerts get their attention call now.
    if incoming_digest:
        for line in list(ops['pending']):
            head = line.rsplit(': ', 1)[0]
            for fp in sorted(table):
                entry = table[fp]
                if fp in attended or not any(head == amod._ops_head(p, entry) for p in amod._ANNOUNCE_PREFIXES):
                    continue
                link = opened_urls.get(fp) or open_urls.get(fp) or (
                    amod.ISSUE_URL_BASE + str(entry['issue']) if entry.get('issue') is not None
                    else amod.ISSUE_URL_BASE.rstrip('/'))
                call_attention(entry['kind'], link, amod.alert_title(entry['kind'], entry['subject']),
                               'freshness alert ' + entry['kind'] + ' is open for ' + entry['subject'])
                attended.add(fp)
    if ops['pending'] and (incoming_digest or not ops['digest_sent']):
        digest = _digest(ops['pending'])
        ops['digest_sent'] = True
        ok = send(_clean(digest), config, run)
        if ok:
            for fp in folded:
                table[fp]['announced'] = True
                table[fp]['last_reminder_at'] = now_iso
        amod.record_send(new, digest, ok, fmod.fingerprint('ops-digest', ops['day']))
    return new
