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
After a state reset the canary URL therefore has only a recorded first
failure on the first night. That recorded failure counts as the dead-link
signal, so that night is not a false alarm, but only when this run wrote it:
`check_links` stamps `first_fail_at` with the run's `now`, so the entry must
be at most `BOOTSTRAP_WINDOW` (one hour) older than the `now` given to
`verify` (the runner passes the same `now`; default: the current UTC time).
An older entry is from an earlier run, so a run that skipped or broke the
link check never passes on it.

Findings have part 1's shape (`freshness/finding.py`); only `kind` and
`skill` are read here. Python 3.9+, standard library only. Importing has no
side effects.
"""

from datetime import datetime, timedelta, timezone

CANARY = 'zz-canary'
CANARY_FILE = '.held/zz-canary/SKILL.md'
CANARY_URL = 'https://github.com/kennylhilljr/skills-catalog/blob/main/.held/zz-canary/never-created.md'
EXPECTED = ('dead-link', 'deprecated-model', 'past-max-age')
BOOTSTRAP_WINDOW = timedelta(hours=1)


def _parse(stamp):
    if isinstance(stamp, datetime):
        return stamp
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def first_failure_recorded(link_history, url=CANARY_URL, now=None, window=BOOTSTRAP_WINDOW):
    """True when this run recorded the first failure of `url` in `link_history`.

    A moved or cleared URL, or one without `first_fail_at`, does not count.
    `first_fail_at` must lie in `[now - window, now]`, where `now` (aware UTC,
    default: the current time) is the run time given to `check_links`.
    """
    entry = (link_history or {}).get(url)
    if not isinstance(entry, dict) or entry.get('moved_to'):
        return False
    first = _parse(entry.get('first_fail_at'))
    if first is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        age = now - first
    except TypeError:  # naive against aware: do not guess
        return False
    return timedelta(0) <= age <= window


def verify(findings, link_history, now=None, url=CANARY_URL):
    """`(ok, reason)`: ok only for exactly the three seeded `zz-canary` findings.

    `findings`: every finding of the run (other skills are ignored).
    `link_history`: the link history after this run's `check_links`.
    `now`: the run time (aware UTC) used for the bootstrap age; the runner
    passes the same `now` it gave `check_links`.
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
    if 'dead-link' not in counts and first_failure_recorded(link_history, url, now):
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
