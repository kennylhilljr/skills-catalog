"""Canary verification: the nightly run must report exactly the three seeded faults.

Requirements: FRS-18, FRS-19 (docs/SPEC.md). Contract: docs/DESIGN.md 7.6
("Dwell limits, alerts and the canary") and 3.2 (held skills).

`.held/zz-canary/SKILL.md` is a held skill with exactly three seeded faults:
a link to a page that never exists (`CANARY_URL`), a model ID that
`freshness/models.json` marks `retired`, and a `catalog-last-verified` far
past its `catalog-max-age-days`. A checker that finds nothing looks the same
as a broken checker, so every run must report exactly one `dead-link`, one
`deprecated-model` and one `past-max-age` finding for `zz-canary`.

`verify(findings, link_history)` returns `(ok, reason)`. The runner (part 9)
records the run as failed and raises a `canary` alert when `ok` is False.

Bootstrap: `freshness/links.py` records the first failure of a URL without a
finding and reports `dead-link` only on a failure at least 20 hours later.
After a state reset the canary URL is inside that soft-fail window for up to
20 hours: every run in it sees the failure but gets no finding. Such a
failure counts as the dead-link signal, so those runs are not false alarms,
but only when this run checked the URL: `check_links` stamps
`last_checked_at` (and, on the first failure, `first_fail_at`) with the
run's `now`, so one of them must equal the `now` given to `verify` (the
runner passes the same value to both), and `first_fail_at` must be less
than `min_gap_hours` before `now`. Without `now` there is no bootstrap. An
entry this run did not stamp, even from a retry minutes before, never
passes, so a run that skipped or broke the link check is always a mismatch,
and a first failure older than the gap should have produced a finding, so
it does not pass either.

Findings have part 1's shape (`freshness/finding.py`); only `kind` and
`skill` are read here. Python 3.9+, standard library only. Importing has no
side effects.
"""

from datetime import datetime, timedelta

CANARY = 'zz-canary'
CANARY_FILE = '.held/zz-canary/SKILL.md'
CANARY_URL = 'https://github.com/kennylhilljr/skills-catalog/blob/main/.held/zz-canary/never-created.md'
EXPECTED = ('dead-link', 'deprecated-model', 'past-max-age')


def _parse(stamp):
    if isinstance(stamp, datetime):
        return stamp
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def first_failure_recorded(link_history, now, url=CANARY_URL, min_gap_hours=20):
    """True when this run saw `url` fail inside the soft-fail window.

    The entry must be stamped by this run (`last_checked_at` or
    `first_fail_at` equal to `now`, the run time given to `check_links`), and
    `first_fail_at` must be at most `now` and less than `min_gap_hours`
    before it. A moved or cleared URL, an entry without `first_fail_at`, or
    `now` None does not count.
    """
    if now is None:
        return False
    entry = (link_history or {}).get(url)
    if not isinstance(entry, dict) or entry.get('moved_to'):
        return False
    first = _parse(entry.get('first_fail_at'))
    if first is None:
        return False
    checked = _parse(entry.get('last_checked_at'))
    try:
        if (first.utcoffset() is None) != (now.utcoffset() is None):
            return False
        if checked is not None and (checked.utcoffset() is None) != (now.utcoffset() is None):
            checked = None
        if first != now and checked != now:
            return False
        return now - timedelta(hours=min_gap_hours) < first <= now
    except TypeError:
        return False


def verify(findings, link_history, now=None, url=CANARY_URL, min_gap_hours=20):
    """`(ok, reason)`: ok only for exactly the three seeded `zz-canary` findings.

    `findings`: every finding of the run (other skills are ignored).
    `link_history`: the link history after this run's `check_links`.
    `now`: the run time the runner gave `check_links`; needed for the
    bootstrap, which counts only a failure stamped with this `now`.
    `min_gap_hours`: the soft-fail gap given to `check_links`.
    `reason` names what was missing or extra; it is 'canary ok' (or notes
    the bootstrap) when `ok` is True.
    """
    counts = {}
    for item in findings or ():
        if not isinstance(item, dict) or item.get('skill') != CANARY:
            continue
        kind = item.get('kind')
        counts[kind] = counts.get(kind, 0) + 1

    bootstrap = False
    if 'dead-link' not in counts and first_failure_recorded(link_history, now, url, min_gap_hours):
        counts['dead-link'] = 1
        bootstrap = True

    missing = [kind for kind in EXPECTED if counts.get(kind, 0) == 0]
    extra = []
    for kind in sorted(counts, key=str):
        surplus = counts[kind] - (1 if kind in EXPECTED else 0)
        if surplus > 0:
            extra.append('%s x%d' % (kind, surplus))

    if missing or extra:
        parts = []
        if missing:
            parts.append('missing: ' + ', '.join(missing))
        if extra:
            parts.append('extra: ' + ', '.join(extra))
        return False, 'canary mismatch for %s (%s); expected exactly one each of %s' % (
            CANARY, '; '.join(parts), ', '.join(EXPECTED))
    if bootstrap:
        return True, 'canary ok (dead link counted from its recorded first failure)'
    return True, 'canary ok'
