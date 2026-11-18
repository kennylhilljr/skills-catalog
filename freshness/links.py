"""Link checks for skills (FRS-5, DESIGN 7.2 "Links").

`extract_links` finds http(s) URLs in a skill's text files. `check_links`
applies the soft-fail rule: the first failure of a URL is only recorded in
`history`; a failure at least `min_gap_hours` after the recorded first failure
yields a `dead-link` finding; a success clears the entry. A 301 or 308 to a
safe target (`safe_location`) records `moved_to` and yields a finding with a
`fix` that replaces the URL on that line (part 5 turns it into a "moved link"
PR). That finding has kind `moved-link`, never `dead-link`. A 301 or 308 to
an unusable target counts as a failure and is never published. A `fix` means: on line `line` of `file`,
replace the text `old` with `new`.

This module does no network work itself: the caller passes `fetch(url) ->
(status, location)`. The real fetcher (part 9's entry point) sends HEAD, then
GET when HEAD gives 405 or 501, with a 10-second timeout, the generic
`User-Agent: skills-catalog-freshness` and no redirect following, and raises
on a network error. `FETCH_TIMEOUT`, `USER_AGENT` and `FALLBACK_TO_GET` hold
those values for it.

Findings have part 1's shape: `{"kind", "skill", "subject", "file", "line",
"evidence", "fix"}`; `subject` is `<file>:<url>`, without a line number.
"""

import importlib.util
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit

from freshness.checks import _is_real_dir, _read_text

_SCRUB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'scrub.py')
_spec = importlib.util.spec_from_file_location('freshness_links_scrub', _SCRUB_PATH)
_scrub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scrub)

FETCH_TIMEOUT = 10
USER_AGENT = 'skills-catalog-freshness'
FALLBACK_TO_GET = (405, 501)
MOVED = (301, 308)
# Kind of the finding a permanent redirect yields; it is not a dead link.
MOVED_KIND = 'moved-link'

# A plain DNS name: two or more labels of at most 63 characters and an
# alphabetic top-level label, so IP literals and numeric hosts are rejected.
_HOSTNAME = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
# URL characters that cannot open or close Markdown or HTML syntax: no
# quotes, angle brackets, backticks, brackets, parentheses, `!`, `*`, `|`,
# braces or backslashes (used with fullmatch).
_PRINTABLE_ASCII = re.compile(r'[A-Za-z0-9._~:/?#@%&=+,;-]+')
_URL = re.compile(r'https?://[^\s<>"\'`]+')
_TRAILING = ').,;>'


def _rel_folder(folder):
    real = os.path.abspath(folder)
    return os.path.basename(os.path.dirname(real)) + '/' + os.path.basename(real)


def extract_links(folder):
    """`[(file, line, url)]` for http(s) URLs in the folder's text files.

    `file` is repo-relative (`skills/<name>/...`). Trailing `).,;>` are
    stripped from each URL.
    """
    base = _rel_folder(folder)
    out = []
    if not _is_real_dir(folder):
        return out
    # os.walk does not descend into symlinked folders, and _read_text skips
    # symlinked, special, binary and oversized files.
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            text = _read_text(path)
            if text is None:
                continue
            rel = base + '/' + os.path.relpath(path, folder).replace(os.sep, '/')
            for number, line in enumerate(text.split('\n'), 1):
                for match in _URL.finditer(line):
                    url = match.group(0).rstrip(_TRAILING)
                    if '://' in url and url.split('://', 1)[1]:
                        out.append((rel, number, url))
    return out


def _parse(stamp):
    return datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp


def _stamp(now):
    return now.isoformat()


def safe_location(url, location):
    """The absolute redirect target, or None when it is not fit to publish.

    The `Location` header comes from a third party and ends up in evidence,
    history and a PR, so it must be a plain http(s) URL that passes the scrub.
    """
    if not isinstance(location, str) or not location:
        return None
    if not _PRINTABLE_ASCII.fullmatch(location):
        return None
    try:
        target = urljoin(url, location)
        parts = urlsplit(target)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not host or len(host) > 253 or not _HOSTNAME.fullmatch(host):
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if port is not None and not 0 < port < 65536:
        return None
    if not _PRINTABLE_ASCII.fullmatch(target) or len(target) > 2048:
        return None
    if _scrub.scan_text(target, 'freshness-link.md'):
        return None
    return target


def _probe(fetch, url):
    """`(ok, status, location)`; a raised error or status >= 400 is a failure."""
    try:
        status, location = fetch(url)
    except Exception as err:  # network errors of any kind count as a failure
        return False, 'error: ' + type(err).__name__, None
    if not isinstance(status, int) or status >= 400:
        return False, status, None
    return True, status, location


def check_links(items, history, fetch, now, min_gap_hours=20):
    """Apply the FRS-5 rule to `items`, a list of `(skill, file, line, url)`.

    `history` maps url to `{"first_fail_at", "last_status", "moved_to"}` and is
    not mutated; the updated copy is returned. Each URL is fetched once per
    call. Returns `(findings, history)`.
    """
    history = {url: dict(entry) for url, entry in (history or {}).items()}
    results = {}
    for _skill, _file, _line, url in items:
        if url not in results:
            results[url] = _probe(fetch, url)

    dead = set()
    for url, (ok, status, location) in results.items():
        if ok and status in MOVED:
            location = safe_location(url, location)
            if location is None:
                # A permanent redirect to a target that cannot be published
                # counts as a failure, so it can still become a dead link.
                ok, status = False, '{} to an unusable target'.format(status)
            results[url] = (ok, status, location)
        if ok and status in MOVED and location:
            history[url] = {'first_fail_at': None, 'last_status': status, 'moved_to': location}
        elif ok:
            history.pop(url, None)
        else:
            entry = history.get(url) or {}
            first = entry.get('first_fail_at')
            if first and now - _parse(first) >= timedelta(hours=min_gap_hours):
                dead.add(url)
            history[url] = {'first_fail_at': first or _stamp(now), 'last_status': status,
                            'moved_to': None}

    findings = []
    for skill, file, line, url in items:
        ok, status, location = results[url]
        subject = file + ':' + url
        if url in dead:
            first = history[url]['first_fail_at']
            findings.append({
                'kind': 'dead-link', 'skill': skill, 'subject': subject, 'file': file,
                'line': line,
                'evidence': '{}:{} links to {}, which failed with {} now and first failed at {}'.format(
                    file, line, url, status, first),
                'fix': None,
            })
        elif ok and status in MOVED and location:
            findings.append({
                'kind': MOVED_KIND, 'skill': skill, 'subject': subject, 'file': file,
                'line': line,
                'evidence': '{}:{} links to {}, which moved permanently ({}) to {}'.format(
                    file, line, url, status, location),
                'fix': {'file': file, 'line': line, 'old': url, 'new': location},
            })
    return findings, history
