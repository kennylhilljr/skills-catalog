"""Open freshness findings for a skill, from the public GitHub search API (MCP-4).

The freshness bot (M2) opens issues on this repository labeled `freshness`,
`re-review`, `upstream-change` or `freshness-alert` with the skill name in the
title (FRS-12, FRS-22). This module only reads them. It uses the standard
library, sends no token, waits at most 5 seconds and caches answers for 10
minutes. The server asks only about served skills, so an excluded name is
never sent.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request

REPO = 'kennylhilljr/skills-catalog'
LABELS = ('freshness', 're-review', 'upstream-change', 'freshness-alert')
SEARCH_URL = 'https://api.github.com/search/issues'
ISSUE_PREFIX = 'https://github.com/' + REPO + '/issues/'
TIMEOUT = 5
CACHE_SECONDS = 600
UNAVAILABLE = 'github unavailable'
_MAX_BYTES = 4 * 1024 * 1024


def title_names(title: str, name: str) -> bool:
    """True when `name` is a whole word of `title` (a hyphen joins words)."""
    pattern = r'(?<![a-z0-9_-])' + re.escape(name.lower()) + r'(?![a-z0-9_-])'
    return re.search(pattern, title.lower()) is not None


def search_query(name: str) -> str:
    return 'repo:%s is:issue is:open label:%s "%s" in:title' % (REPO, ','.join(LABELS), name)


class GitHubFindings:
    """The default findings fetcher: `fetcher(name)` returns `[{url, title}]`.

    It raises on any failure; `lookup()` turns that into `([], reason)`.
    `urlopen` and `clock` are injectable for tests.
    """

    def __init__(self, urlopen=None, clock=time.monotonic, timeout=TIMEOUT, ttl=CACHE_SECONDS):
        self._urlopen = urlopen
        self._clock = clock
        self._timeout = timeout
        self._ttl = ttl
        self._cache = {}
        self._lock = threading.Lock()

    def __call__(self, name: str) -> list:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(name)
            if hit is not None and now - hit[0] < self._ttl:
                return [dict(item) for item in hit[1]]
        items = self._fetch(name)
        with self._lock:
            self._cache[name] = (now, items)
        return [dict(item) for item in items]

    def _fetch(self, name):
        url = SEARCH_URL + '?' + urllib.parse.urlencode({'q': search_query(name), 'per_page': '100'})
        request = urllib.request.Request(url, headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'skills-catalog-mcp',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        urlopen = self._urlopen or urllib.request.urlopen
        with urlopen(request, timeout=self._timeout) as response:
            status = getattr(response, 'status', 200)
            if status != 200:
                raise OSError('status %s' % status)
            body = response.read(_MAX_BYTES + 1)
        if len(body) > _MAX_BYTES:
            raise ValueError('response too large')
        data = json.loads(body.decode('utf-8'))
        found = []
        for item in data['items']:
            url, title = item.get('html_url'), item.get('title')
            if not isinstance(url, str) or not isinstance(title, str):
                continue
            if 'pull_request' in item or not url.startswith(ISSUE_PREFIX):
                continue
            if title_names(title, name):
                found.append({'url': url, 'title': title})
        found.sort(key=lambda f: f['url'])
        return found


def lookup(fetcher, name):
    """Return `(findings, error)` from `fetcher(name)`; never raises.

    On any failure, or an answer that is not a list of `{url, title}`, it
    returns `([], 'github unavailable')`.
    """
    try:
        raw = fetcher(name)
        findings = [{'url': str(item['url']), 'title': str(item['title'])} for item in raw]
    except Exception:  # noqa: BLE001 - the tool must answer whatever the fetcher does
        return [], UNAVAILABLE
    return findings, None
