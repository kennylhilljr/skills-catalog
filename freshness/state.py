"""The freshness state file, `freshness/state.json` on the `freshness-state` branch.

Requirements: FRS-24, FRS-25, FRS-27 (docs/SPEC.md). Contract: docs/DESIGN.md
7.6 and 7.8. Part 9 writes the branch; this module only parses and prints.

Each part owns its own top-level keys, and unknown keys are kept as they are:
`heartbeats` (part 9), `alerts`, `ops`, `issues` and `findings` (part 1),
`links` (part 2), `sources` (part 3), `cli` (part 4), `classifier` (part 5).

Keys owned here::

    alerts[fp] = {"kind": str, "subject": str, "issue": int | None,
                  "evidence_sha": str, "last_comment_at": iso | None,
                  "last_reminder_at": iso | None, "announced": bool,
                  "clean_runs": [run_id], "closed_at": iso | None}
    ops = {"day": "YYYY-MM-DD", "sent": int, "digest_sent": bool,
           "pending": [text], "failed": [text]}
    issues = {"day": "YYYY-MM-DD", "opened": int, "storm_issue": int | None}
    findings[fp] = {"evidence_sha": str}

All times are timezone-aware UTC ISO 8601 strings. Python 3.9+, standard
library only. Importing has no side effects.
"""

import datetime
import json

UTC = datetime.timezone.utc


def _empty_ops():
    return {'day': '', 'sent': 0, 'digest_sent': False, 'pending': [], 'failed': []}


def _empty_issues():
    return {'day': '', 'opened': 0, 'storm_issue': None}


def empty_state():
    return {'alerts': {}, 'findings': {}, 'issues': _empty_issues(), 'ops': _empty_ops()}


def load(text):
    """Parse state text; empty text gives `empty_state()`. Unknown keys are kept."""
    if text is None or not text.strip():
        return empty_state()
    state = json.loads(text)
    if not isinstance(state, dict):
        raise ValueError('state.json must hold a JSON object')
    state.setdefault('alerts', {})
    state.setdefault('findings', {})
    issues = state.setdefault('issues', _empty_issues())
    for key, value in _empty_issues().items():
        issues.setdefault(key, value)
    ops = state.setdefault('ops', _empty_ops())
    for key, value in _empty_ops().items():
        ops.setdefault(key, value)
    return state


def dump(state):
    """Sorted keys, 2-space indent, trailing newline."""
    return json.dumps(state, sort_keys=True, indent=2, ensure_ascii=False) + '\n'


def require_utc(now):
    """`now` as an aware UTC datetime; a naive datetime is an error."""
    if not isinstance(now, datetime.datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be a timezone-aware datetime')
    return now.astimezone(UTC)


def iso(now):
    return require_utc(now).isoformat(timespec='seconds')


def parse_iso(text):
    """Aware UTC datetime from an ISO string, or None for None/empty."""
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    value = datetime.datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def day_of(now):
    return require_utc(now).date().isoformat()


def roll_day(state, now):
    """Reset the daily counters when `now` is on a new UTC day; `failed` is kept."""
    day = day_of(now)
    ops = state.setdefault('ops', _empty_ops())
    if ops.get('day') != day:
        failed = list(ops.get('failed') or [])
        ops.clear()
        ops.update(_empty_ops())
        ops['day'] = day
        ops['failed'] = failed
    issues = state.setdefault('issues', _empty_issues())
    if issues.get('day') != day:
        issues.clear()
        issues.update(_empty_issues())
        issues['day'] = day
    return state
