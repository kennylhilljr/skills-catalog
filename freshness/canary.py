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
equal the `now` given to `verify` (the runner passes the same value to both).
Without `now` there is no bootstrap. An entry from an earlier run, even a
retry minutes before, never passes, so a run that skipped or broke the link
check is always a mismatch.

Findings have part 1's shape (`freshness/finding.py`); only `kind` and
`skill` are read here. Python 3.9+, standard library only. Importing has no
side effects.
"""

from datetime import datetime

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


def first_failure_recorded(link_history, now, url=CANARY_URL):
    """True when this run recorded the first failure of `url` in `link_history`.

    `first_fail_at` must equal `now`, the run time given to `check_links`.
    A moved or cleared URL, an entry without `first_fail_at`, or `now` None
    does not count.
    """
    if now is None:
        return False
    entry = (link_history or {}).get(url)
    if not isinstance(entry, dict) or entry.get('moved_to'):
        return False
    first = _parse(entry.get('first_fail_at'))
    if first is None:
        return False
    try:
        return first == now and (first.utcoffset() is None) == (now.utcoffset() is None)
    except TypeError:
        return False


def verify(findings, link_history, now=None, url=CANARY_URL):
    """`(ok, reason)`: ok only for exactly the three seeded `zz-canary` findings.

    `findings`: every finding of the run (other skills are ignored).
    `link_history`: the link history after this run's `check_links`.
    `now`: the run time the runner gave `check_links`; needed for the
    bootstrap, which counts only a first failure stamped with this `now`.
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
    if 'dead-link' not in counts and first_failure_recorded(link_history, now, url):
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
