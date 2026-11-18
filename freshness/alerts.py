"""Alert rendering and the alert and finding planner of the freshness bot.

Requirements: FRS-13, FRS-22, FRS-24, FRS-25, FRS-27 (docs/SPEC.md).
Contract: docs/DESIGN.md 7.5 and 7.8.

This module is pure logic. It decides what to do and returns Actions
(see freshness/finding.py); part 5 carries them out on GitHub and part 7
sends the ops messages. `now` is always a parameter and timezone-aware.

Ops message text for an issue opened in the same run cannot know the issue
number yet. It then holds ISSUE_URL_PLACEHOLDER, which the executor replaces
with the URL of the issue it opened for the action's fingerprint.

Python 3.9+, standard library only. Importing has no side effects.
"""

import copy
import datetime
import importlib.util
import os

from . import finding as fmod
from . import state as smod

ALERT_LABEL = 'freshness-alert'
TITLE_MAX = 100
ISSUES_PER_DAY = 5
OPS_PER_DAY = 6
COOLDOWN = datetime.timedelta(hours=24)
REOPEN_WINDOW = datetime.timedelta(days=7)
CLEAN_RUNS_TO_CLOSE = 2

ISSUE_URL_BASE = 'https://github.com/kennylhilljr/skills-catalog/issues/'
ISSUE_URL_PLACEHOLDER = '{issue_url}'
DIGEST_PREFIX = 'skills-catalog freshness digest: '

DEFAULT_WHAT_TO_CHANGE = 'investigate; a no-code answer that names the fix is acceptable'
DEFAULT_ACCEPTANCE = 'none: closes when the next run is clean'

SECTIONS = ('What happened', 'Evidence', 'What to change', 'Files', 'Acceptance')

_SCRUB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'scrub.py')
_scrub_module = None


def _scan(text):
    global _scrub_module
    if _scrub_module is None:
        spec = importlib.util.spec_from_file_location('freshness_tools_scrub', _SCRUB_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _scrub_module = module
    return _scrub_module.scan_text(text)


def _scan_file(path, text, private_names=()):
    global _scrub_module
    _scan('')
    return _scrub_module.scan_text(text, path, private_names)


def scrub_text(text):
    """`text` with every line that `tools/scrub.scan_text` flags withheld.

    Everything the bot writes to GitHub or sends as an ops message passes
    through here, so a secret, home path or address in evidence is never
    published. The withheld line names only the finding kinds.
    """
    if not text:
        return text
    found = {}
    for number, kind in _scan(text):
        found.setdefault(number, []).append(kind)
    if not found:
        return text
    lines = text.split('\n')
    for number, kinds in found.items():
        lines[number - 1] = '[line withheld by the scrub: ' + ', '.join(sorted(kinds)) + ']'
    return '\n'.join(lines)


def _scrub_actions(actions):
    for action in actions:
        for key in ('title', 'body', 'text'):
            if isinstance(action.get(key), str):
                action[key] = scrub_text(action[key])
    return actions


def alert_labels(kind):
    """Labels of an alert issue: never bot:skip or bot:stop (FRS-22)."""
    return [ALERT_LABEL, kind]


def alert_title(kind, subject):
    title = ('freshness-alert: ' + kind + ': ' + subject)[:TITLE_MAX]
    if _scan(title):
        title = 'freshness-alert: ' + kind + ': subject withheld by the scrub'
    return title


def _block(value, empty):
    if value is None:
        return empty
    if isinstance(value, str):
        return value.strip() or empty
    return '\n'.join(str(v) for v in value) or empty


def _files_block(files):
    if not files:
        return 'none'
    if isinstance(files, str):
        files = [files]
    return '\n'.join('- `' + f + '`' for f in files)


def _acceptance_block(acceptance):
    if not acceptance:
        return DEFAULT_ACCEPTANCE
    if isinstance(acceptance, str):
        return acceptance.strip()
    return '\n\n'.join('```\n' + cmd + '\n```' for cmd in acceptance)


def render_alert(kind, subject, what_happened, evidence, what_to_change, files, acceptance):
    """Title and body of a freshness-alert issue (FRS-22, DESIGN 7.8).

    `files` is a list of repo-relative paths; `acceptance` is a string or a
    list of `python3 -B -c` probes. The body ends with the fingerprint marker.
    """
    parts = [
        ('What happened', _block(what_happened, 'see Evidence')),
        ('Evidence', _block(evidence, 'none')),
        ('What to change', _block(what_to_change, DEFAULT_WHAT_TO_CHANGE)),
        ('Files', _files_block(files)),
        ('Acceptance', _acceptance_block(acceptance)),
    ]
    body = '\n\n'.join('## ' + name + '\n\n' + text for name, text in parts)
    body += '\n\n' + fmod.marker(fmod.fingerprint(kind, subject)) + '\n'
    return alert_title(kind, subject), body


def _render(alert):
    return render_alert(
        alert['kind'],
        alert['subject'],
        alert.get('what_happened'),
        alert.get('evidence', ''),
        alert.get('what_to_change'),
        alert.get('files'),
        alert.get('acceptance'),
    )


def _new_entry(kind, subject, issue, sha, now_iso):
    return {
        'kind': kind,
        'subject': scrub_text(subject),
        'issue': issue,
        'evidence_sha': sha,
        'last_comment_at': now_iso,
        'last_reminder_at': None,
        'announced': False,
        'clean_runs': [],
        'closed_at': None,
    }


def _elapsed(since_iso, now, limit):
    """True when `since_iso` is unset or at least `limit` before `now`."""
    since = smod.parse_iso(since_iso)
    return since is None or now - since >= limit


def _url(number):
    return ISSUE_URL_BASE + str(number) if number is not None else ISSUE_URL_PLACEHOLDER


def _ops_text(prefix, entry):
    return _ops_head(prefix, entry) + ': ' + _url(entry['issue'])


def _latest_closed(closed_issues):
    latest = {}
    for issue in closed_issues or ():
        fp = issue.get('fingerprint')
        when = smod.parse_iso(issue.get('closed_at'))
        if not fp or when is None:
            continue
        if fp not in latest or when > latest[fp][0]:
            latest[fp] = (when, issue)
    return latest


def _storm(folded, day, table, open_by_fp, closed_by_fp, issues, actions, raised, fresh, run_id, now):
    """Fold the 6th and later new alerts of a day into one storm issue (FRS-25)."""
    kind, subject = 'storm', day
    fp = fmod.fingerprint(kind, subject)
    lines = sorted(set('- ' + a['kind'] + ': ' + a['subject'] for a in folded))
    evidence = '\n'.join(lines)
    sha = fmod.evidence_sha(evidence)
    now_iso = smod.iso(now)
    raised[fp] = {'kind': kind, 'subject': subject}
    entry = table.get(fp)
    if fp in open_by_fp:
        number = open_by_fp[fp]['number']
        issues['storm_issue'] = number
        if entry is None or entry.get('closed_at'):
            entry = table[fp] = dict(_new_entry(kind, subject, number, '', None), announced=bool(entry and entry.get('announced')))
        entry['issue'] = number
        entry['clean_runs'] = []
        # No 24-hour cooldown here: the storm fingerprint changes every day,
        # so a held-back list would never be posted. Each comment lists
        # distinct alerts, at most one per run.
        if entry['evidence_sha'] != sha:
            actions.append({'op': 'comment', 'fingerprint': fp, 'number': number,
                            'body': 'More alerts folded into this storm in run ' + run_id + ':\n\n' + evidence})
            entry['evidence_sha'] = sha
            entry['last_comment_at'] = now_iso
        return
    closed = closed_by_fp.get(fp)
    if closed and now - closed[0] < REOPEN_WINDOW:
        number = closed[1]['number']
        actions.append({'op': 'reopen', 'fingerprint': fp, 'number': number})
        actions.append({'op': 'comment', 'fingerprint': fp, 'number': number,
                        'body': 'Storm raised again in run ' + run_id + ':\n\n' + evidence})
        table[fp] = _new_entry(kind, subject, number, sha, now_iso)
        issues['storm_issue'] = number
        fresh[fp] = 'reopened'
        return
    title, body = render_alert(
        kind, subject,
        'More than ' + str(ISSUES_PER_DAY) + ' new freshness alerts on ' + day + ' (run ' + run_id + '). '
        'The alerts below were folded into this issue instead of opening one issue each, '
        'since a burst usually has one cause.',
        evidence,
        'investigate the common cause of the alerts listed under Evidence',
        [],
        None,
    )
    actions.append({'op': 'open_issue', 'fingerprint': fp, 'title': title, 'body': body,
                    'labels': alert_labels(kind)})
    table[fp] = _new_entry(kind, subject, None, sha, None)
    issues['storm_issue'] = None
    fresh[fp] = 'opened'


def plan_alerts(alerts, open_issues, closed_issues, state, run_id, now, scope=None, announce=True):
    """Plan the issue actions and ops messages for this run's alerts.

    `alerts`: the alerts raised this run, each `{"kind", "subject",
    "evidence", "what_happened"?, "what_to_change"?, "files"?, "acceptance"?}`.
    `open_issues` and `closed_issues`: `{"number", "fingerprint",
    "closed_at"}` parsed from the fingerprint marker of alert issues.
    `scope`: the alert kinds this run checks, or None for all; an open alert
    of another kind is not counted clean by this run (the Actions and Mac
    halves raise different kinds). `announce=False` plans no ops messages
    and leaves the ops state alone (the Actions half only opens issues,
    FRS-23).

    Returns `(actions, new_state)`; the given state is not changed.
    Rules: FRS-24 (dedup, comment cooldown, reopen window, reminders),
    FRS-25 (5 issues and 6 ops messages a day, storm issue, digest, retry of
    failed sends) and FRS-27 (close after 2 consecutive clean runs).
    """
    now = smod.require_utc(now)
    now_iso = smod.iso(now)
    new = copy.deepcopy(state) if state is not None else smod.empty_state()
    smod.roll_day(new, now)
    table = new.setdefault('alerts', {})
    ops, issues = new['ops'], new['issues']
    day = ops['day']

    open_by_fp = {}
    for issue in open_issues or ():
        if issue.get('fingerprint'):
            open_by_fp[issue['fingerprint']] = issue
    closed_by_fp = _latest_closed(closed_issues)
    for fp, entry in table.items():
        if fp in open_by_fp:
            entry['issue'] = open_by_fp[fp]['number']

    actions = []
    raised = {}
    fresh = {}      # fp -> 'opened' | 'reopened' this run
    reminders = []  # fps due a reminder
    folded = []

    for alert in alerts or ():
        if alert.get('kind') == 'storm':
            continue  # the storm alert is raised only by this planner
        fp = fmod.fingerprint(alert['kind'], alert['subject'])
        if fp in raised:
            continue
        raised[fp] = alert
        sha = fmod.evidence_sha(alert.get('evidence', ''))
        entry = table.get(fp)

        if fp in open_by_fp:
            number = open_by_fp[fp]['number']
            if entry is None:
                entry = table[fp] = _new_entry(alert['kind'], alert['subject'], number, sha, None)
            elif entry.get('closed_at'):
                entry['closed_at'] = None
            entry['issue'] = number
            entry['clean_runs'] = []
            if entry['evidence_sha'] != sha and _elapsed(entry.get('last_comment_at'), now, COOLDOWN):
                actions.append({'op': 'comment', 'fingerprint': fp, 'number': number,
                                'body': 'Evidence changed in run ' + run_id + ':\n\n' + alert.get('evidence', '')})
                entry['evidence_sha'] = sha
                entry['last_comment_at'] = now_iso
            if entry.get('announced') and _elapsed(entry.get('last_reminder_at'), now, COOLDOWN):
                reminders.append(fp)
            continue

        closed = closed_by_fp.get(fp)
        if closed and now - closed[0] < REOPEN_WINDOW:
            number = closed[1]['number']
            actions.append({'op': 'reopen', 'fingerprint': fp, 'number': number})
            actions.append({'op': 'comment', 'fingerprint': fp, 'number': number,
                            'body': 'Raised again in run ' + run_id + ' within 7 days of closing, '
                                    'so this issue is reopened instead of opening a new one.\n\n'
                                    '## Evidence\n\n' + alert.get('evidence', '')})
            table[fp] = _new_entry(alert['kind'], alert['subject'], number, sha, now_iso)
            fresh[fp] = 'reopened'
            continue

        if issues['opened'] < ISSUES_PER_DAY:
            title, body = _render(alert)
            actions.append({'op': 'open_issue', 'fingerprint': fp, 'title': title, 'body': body,
                            'labels': alert_labels(alert['kind'])})
            issues['opened'] += 1
            table[fp] = _new_entry(alert['kind'], alert['subject'], None, sha, None)  # no comment yet
            fresh[fp] = 'opened'
        else:
            folded.append(alert)
            raised.pop(fp)

    if folded:
        _storm(folded, day, table, open_by_fp, closed_by_fp, issues, actions, raised, fresh, run_id, now)

    # FRS-27: count clean runs and close after 2 in a row.
    for fp in sorted(table):
        entry = table[fp]
        if fp in raised or entry.get('closed_at'):
            continue
        if scope is not None and entry.get('kind') != 'storm' and entry.get('kind') not in scope:
            continue
        issue = open_by_fp.get(fp)
        if issue is None:
            closed = closed_by_fp.get(fp)
            if closed:
                entry['closed_at'] = smod.iso(closed[0])
            elif entry.get('issue') is None:
                del table[fp]  # never seen open and no longer raised
            else:
                entry['closed_at'] = now_iso
            continue
        entry['issue'] = issue['number']
        runs = entry.setdefault('clean_runs', [])
        if not runs or runs[-1] != run_id:
            runs.append(run_id)
        if len(runs) >= CLEAN_RUNS_TO_CLOSE:
            pair = runs[-CLEAN_RUNS_TO_CLOSE:]
            actions.append({'op': 'comment', 'fingerprint': fp, 'number': issue['number'],
                            'body': 'The condition was clear on ' + str(CLEAN_RUNS_TO_CLOSE)
                                    + ' consecutive runs: ' + ' and '.join(pair) + '. Closing.'})
            actions.append({'op': 'close', 'fingerprint': fp, 'number': issue['number']})
            entry['closed_at'] = now_iso

    if announce:
        actions.extend(_plan_ops(new, table, open_by_fp, fresh, reminders, now_iso))
    return _scrub_actions(actions), new


def _plan_ops(new, table, open_by_fp, fresh, reminders, now_iso):
    """Ops messages under the daily cap, with one digest (FRS-23, FRS-24, FRS-25)."""
    ops = new['ops']
    candidates = []  # (fingerprint, text, on_queued)

    for text in list(ops.get('failed') or []):
        def retried(text=text):
            if text in ops['failed']:
                ops['failed'].remove(text)
        candidates.append((fmod.fingerprint('ops-retry', text), text, retried))

    for fp in sorted(table):
        entry = table[fp]
        if entry.get('closed_at') or entry.get('announced'):
            continue
        if fp not in fresh and fp not in open_by_fp:
            continue
        prefix = 'freshness-alert reopened' if fresh.get(fp) == 'reopened' else 'freshness-alert'

        def announced(entry=entry):
            entry['announced'] = True
            entry['last_reminder_at'] = now_iso
        candidates.append((fp, _ops_text(prefix, entry), announced))

    for fp in reminders:
        entry = table[fp]

        def reminded(entry=entry):
            entry['last_reminder_at'] = now_iso
        candidates.append((fp, _ops_text(_REMINDER_PREFIX, entry), reminded))

    actions = []
    for fp, text, on_queued in candidates:
        if ops['sent'] < OPS_PER_DAY:
            actions.append({'op': 'send_ops', 'fingerprint': fp, 'text': text})
            ops['sent'] += 1
            on_queued()
        elif not ops['digest_sent']:
            ops['pending'].append(scrub_text(text.replace(ISSUE_URL_PLACEHOLDER, 'new issue')))
            on_queued()
        # After the digest, nothing more is sent that day; the item stays due.
    if ops['pending'] and not ops['digest_sent']:
        digest = (DIGEST_PREFIX + 'daily cap of ' + str(OPS_PER_DAY) + ' ops messages reached, '
                  + str(len(ops['pending'])) + ' more:\n' + '\n'.join('- ' + t for t in ops['pending']))
        actions.append({'op': 'send_ops', 'fingerprint': fmod.fingerprint('ops-digest', ops['day']),
                        'text': digest})
        ops['digest_sent'] = True
    return actions


def plan_findings(findings, open_items, state, private_names=()):
    """One issue or PR per finding fingerprint (FRS-13).

    `findings`: routed items `{"finding", "as": "issue"|"pr", "title",
    "body", "labels", "files"}` from part 5's router. `open_items`: open
    issues and PRs `{"number", "fingerprint"}` parsed from the marker.
    An open item with the fingerprint gets nothing when the evidence is
    unchanged and one comment when it changed. `state["findings"]` records
    the last evidence hash per fingerprint and is updated in place.
    `private_names`: the private repo list the scrub checks PR files
    against (read by the caller, like `tools/scrub.py --private-repos`);
    a PR whose files fail the scrub becomes an issue without the files.
    """
    table = state.setdefault('findings', {})
    open_by_fp = {}
    for item in open_items or ():
        if item.get('fingerprint'):
            open_by_fp[item['fingerprint']] = item
    actions = []
    seen = set()
    for item in findings or ():
        found = item['finding']
        fp = fmod.finding_fingerprint(found)
        if fp in seen:
            continue
        seen.add(fp)
        sha = fmod.evidence_sha(found.get('evidence', ''))
        record = table.get(fp)
        if fp in open_by_fp:
            if record is None:
                table[fp] = {'evidence_sha': sha}
            elif record.get('evidence_sha') != sha:
                actions.append({'op': 'comment', 'fingerprint': fp, 'number': open_by_fp[fp]['number'],
                                'body': 'Evidence changed:\n\n' + found.get('evidence', '')})
                record['evidence_sha'] = sha
            continue
        body = fmod.strip_markers(item.get('body') or '')
        body = body.rstrip('\n') + '\n\n' + fmod.marker(fp) + '\n'
        action = {'fingerprint': fp, 'title': item['title'], 'body': body,
                  'labels': list(item.get('labels') or [])}
        as_ = item.get('as')
        files = dict(item.get('files') or {})
        if as_ == 'pr' and any(_scan_file(path, text, private_names) for path, text in files.items()):
            as_ = 'issue'  # never publish a fix the scrub rejects; a person decides
        if as_ == 'pr':
            action.update(op='open_pr', files=files, branch=fmod.branch_name(fp))
        elif as_ == 'issue':
            action['op'] = 'open_issue'
        else:
            raise ValueError('routed item "as" must be "issue" or "pr"')
        actions.append(action)
        table[fp] = {'evidence_sha': sha}
    return _scrub_actions(actions)


_ANNOUNCE_PREFIXES = ('freshness-alert', 'freshness-alert reopened')
_REMINDER_PREFIX = 'freshness-alert still open'


def _ops_head(prefix, entry):
    return 'skills-catalog ' + prefix + ': ' + entry['kind'] + ' ' + entry['subject']


def _reset_alert_message(state, text, fingerprint=None):
    """Mark the alerts behind a failed ops message as due again; False if none.

    Without a fingerprint every alert whose message matches is reset, since
    two subjects can scrub to the same text; at worst one is sent twice.
    """
    table = state.get('alerts') or {}
    head = text.rsplit(': ', 1)[0]
    entries = [table[fingerprint]] if fingerprint in table else list(table.values())
    matched = False
    for entry in entries:
        if head == _ops_head(_REMINDER_PREFIX, entry):
            entry['last_reminder_at'] = None
            matched = True
        elif any(head == _ops_head(p, entry) for p in _ANNOUNCE_PREFIXES):
            entry['announced'] = False
            entry['last_reminder_at'] = None
            matched = True
    return matched


def record_send(state, text, ok, fingerprint=None):
    """Record one `openclaw message send` result (FRS-25); a failure is retried next run.

    Called by part 7 right after each send of the same run, with the
    action's `fingerprint`. A failed message gives its slot under the daily
    cap back. A failed alert announcement or reminder marks that alert as
    due again, so the next run sends it with the current issue URL, and not
    at all once the alert has closed (FRS-27). Any other failed text goes to
    `failed`, which the next run sends first. A failed digest does the same
    for each of its lines, so nothing in it is lost at the day rollover.
    """
    if ok:
        return state
    ops = state.setdefault('ops', {})
    failed = ops.setdefault('failed', [])
    if text.startswith(DIGEST_PREFIX):
        ops['digest_sent'] = False
        for line in ops.get('pending') or []:
            if not _reset_alert_message(state, line) and line not in failed:
                failed.append(line)
        ops['pending'] = []
        return state
    ops['sent'] = max(0, int(ops.get('sent') or 0) - 1)
    if not _reset_alert_message(state, text, fingerprint) and text not in failed:
        failed.append(text)
    return state
