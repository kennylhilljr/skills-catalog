"""Dwell limits: every state the freshness bot can sit in has a maximum time and an alert.

Requirements: FRS-17, INV-6 (docs/SPEC.md). Contract: docs/DESIGN.md 7.6
(the dwell table) and 7.8 (alerts).

`LIMITS` is the DESIGN 7.6 table as data, one row per state. `STATES` is
every state name the runner (part 9) records; a test requires a `LIMITS`
row with a positive limit and an alert kind for each (INV-6).

`check_dwell(state, open_items, now, milestone)` returns one alert per item
past an enabled row, with subject `<state>:<item>`, so each stuck item has
its own `freshness-alert` issue (part 1's `alerts.plan_alerts`, which
also sends the ops messages and applies the daily caps). Items are::

    {"id": str | int, "since": iso}     # rows with max_hours: the start of the dwell
    {"id": str | int, "runs": int}      # rows with max_runs: consecutive runs in the state
    "pinged_at": iso | None             # rows with ping_first: when the PR was pinged

A freshness PR open without review is first pinged with a comment on the PR
(`ping_actions`), and the alert follows on a later run.

Python 3.9+, standard library only. Importing has no side effects.
"""

import datetime

from . import alerts as amod
from . import state as smod

MILESTONES = ('M1', 'M2', 'M3', 'M4', 'M5', 'M6')

LIMITS = (
    {'state': 'actions-run', 'max_hours': 26, 'alert_kind': 'heartbeat-actions', 'enabled_from': 'M2',
     'what': 'Nightly Actions run not completed'},
    {'state': 'mac-job', 'max_hours': 26, 'alert_kind': 'heartbeat-mac', 'enabled_from': 'M2',
     'what': 'Mac launchd job not completed (Mac asleep counts)'},
    {'state': 'source-failing', 'max_runs': 3, 'alert_kind': 'source-failing', 'enabled_from': 'M2',
     'what': 'A watched source failing to fetch'},
    {'state': 'freshness-pr-unreviewed', 'max_hours': 7 * 24, 'alert_kind': 'dwell', 'enabled_from': 'M2',
     'ping_first': True, 'what': 'Freshness PR open without review'},
    {'state': 'freshness-issue-unpicked', 'max_hours': 3 * 24, 'alert_kind': 'dwell', 'enabled_from': 'M5',
     'what': 'Freshness issue not picked up by iwb'},
    {'state': 're-review', 'max_hours': 14 * 24, 'alert_kind': 'dwell', 'enabled_from': 'M2',
     'what': 'Skill past max age, in re-review (the stale PR comes from freshness/age.py)'},
    {'state': 'consumer-bump-pr', 'max_hours': 7 * 24, 'alert_kind': 'dwell', 'enabled_from': 'M5',
     'what': 'Bump PR open in a consumer'},
)

STATES = tuple(row['state'] for row in LIMITS)
_BY_STATE = {row['state']: row for row in LIMITS}

PING_KEY = 'dwell_pings'  # state["dwell_pings"][str(pr number)] = ping ISO time (owned here)


def limit(state):
    """The `LIMITS` row of a state; an unknown state is an error."""
    if state not in _BY_STATE:
        raise ValueError('unknown dwell state: %r' % (state,))
    return _BY_STATE[state]


def enabled(row, milestone='M2'):
    return MILESTONES.index(row['enabled_from']) <= MILESTONES.index(milestone)


def _limit_text(row):
    if 'max_runs' in row:
        return '%d consecutive runs' % row['max_runs']
    hours = row['max_hours']
    return '%d days' % (hours // 24) if hours % 24 == 0 and hours >= 72 else '%d hours' % hours


def _alert(row, item, evidence):
    return {
        'kind': row['alert_kind'],
        'subject': '%s:%s' % (row['state'], item['id']),
        'evidence': evidence,
        'what_happened': '%s: `%s` exceeded its dwell limit of %s (FRS-17, INV-6).'
                         % (row['what'], item['id'], _limit_text(row)),
        'what_to_change': amod.DEFAULT_WHAT_TO_CHANGE,
        'files': [],
        'acceptance': amod.DEFAULT_ACCEPTANCE,
    }


def check_dwell(state, open_items, now, milestone='M2'):
    """One alert per item of `state` past its dwell limit; [] when the row is not enabled yet."""
    row = limit(state)
    if milestone not in MILESTONES:
        raise ValueError('unknown milestone: %r' % (milestone,))
    if not enabled(row, milestone):
        return []
    now = smod.require_utc(now)
    alerts = []
    for item in open_items or ():
        if 'max_runs' in row:
            runs = int(item.get('runs') or 0)
            if runs >= row['max_runs']:
                alerts.append(_alert(row, item, '%d consecutive runs in state `%s` (limit %d).'
                                     % (runs, state, row['max_runs'])))
            continue
        since = smod.parse_iso(item.get('since'))
        if since is None:
            continue
        hours = (now - since).total_seconds() / 3600
        if hours <= row['max_hours']:
            continue
        if row.get('ping_first') and not item.get('pinged_at'):
            continue  # ping_actions comments on the PR first
        evidence = 'In state `%s` since %s, %.1f hours (limit %d).' % (state, smod.iso(since), hours,
                                                                       row['max_hours'])
        if item.get('pinged_at'):
            evidence += ' Pinged on the PR at %s.' % item['pinged_at']
        alerts.append(_alert(row, item, evidence))
    return alerts


def ping_actions(open_prs, state_doc, now):
    """Comment once on each freshness PR open without review past 7 days.

    `open_prs`: `[{"id": number, "fingerprint", "since": iso}]`. Returns
    `(actions, items)`: the comment actions and the items with `pinged_at`
    filled from `state_doc["dwell_pings"]`, ready for `check_dwell`. Pings
    of PRs that are no longer open are dropped. `state_doc` is changed in
    place.
    """
    row = limit('freshness-pr-unreviewed')
    now = smod.require_utc(now)
    pings = state_doc.setdefault(PING_KEY, {})
    open_ids = {str(item['id']) for item in open_prs or ()}
    for key in list(pings):
        if key not in open_ids:
            del pings[key]
    actions, items = [], []
    for item in open_prs or ():
        key = str(item['id'])
        since = smod.parse_iso(item.get('since'))
        if key not in pings and since is not None and now - since > datetime.timedelta(hours=row['max_hours']):
            actions.append({'op': 'comment', 'fingerprint': item['fingerprint'], 'number': int(item['id']),
                            'body': 'This freshness PR has been open without review for more than 7 days '
                                    '(FRS-17). Please review it; a `freshness-alert` follows if it stays open.'})
            pings[key] = smod.iso(now)
            items.append(dict(item, pinged_at=None))  # the alert waits for a later run
            continue
        items.append(dict(item, pinged_at=pings.get(key)))
    return actions, items
