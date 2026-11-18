"""Catalog tools: search_skills and skill_status (MCP-3, MCP-4, MCP-8, DESIGN 4.4).

Both tools answer only from the snapshot, which holds the served skills of
one tag: held, canary and excluded skills are not in it, so they can never
match a search, and asking for one gets the same `unknown skill` error as a
name that never existed (INV-7, CAT-20). No error echoes a requested name.
"""

from __future__ import annotations

import os
import re

import yaml
from fastmcp.exceptions import ToolError

from skills_catalog_mcp import findings, version

MAX_RESULTS = 20
UNKNOWN_SKILL = 'unknown skill'
EMPTY_QUERY = 'query is empty'
NO_THIRD_PARTY = 'third-party index is not available'
UNKNOWN_SCOPE = 'unknown scope'
_WORD = re.compile(r'[a-z0-9]+')


def _words(text):
    return set(_WORD.findall(str(text).lower()))


def _description(root, name):
    """The `description` from a served SKILL.md's frontmatter, or ''."""
    try:
        with open(os.path.join(root, name, 'SKILL.md'), encoding='utf-8') as handle:
            text = handle.read()
        if not text.startswith('---'):
            return ''
        end = text.find('\n---', 3)
        meta = yaml.safe_load(text[3:end]) if end != -1 else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return ''
    value = meta.get('description') if isinstance(meta, dict) else None
    return ' '.join(value.split()) if isinstance(value, str) else ''


class Catalog:
    """The served skills of one snapshot and the two tool bodies."""

    def __init__(self, snapshot, findings_fetcher=None):
        self.snapshot = snapshot
        self.entries = {e['name']: e for e in snapshot.catalog}
        self.descriptions = {name: _description(snapshot.root, name) for name in self.entries}
        self.fetcher = findings_fetcher if findings_fetcher is not None else findings.GitHubFindings()

    def search(self, query, scope='catalog'):
        if scope == 'third-party':
            raise ToolError(NO_THIRD_PARTY)
        if scope != 'catalog':
            raise ToolError(UNKNOWN_SCOPE)
        wanted = _words(query or '')
        if not wanted:
            raise ToolError(EMPTY_QUERY)
        hits = []
        for name in self.entries:
            if wanted & set(name.split('-')):
                rank = 0
            elif wanted & _words(self.descriptions[name]):
                rank = 1
            else:
                continue
            hits.append((rank, name))
        hits.sort()
        return [
            {'name': name, 'description': self.descriptions[name], 'uri': 'skill://%s/SKILL.md' % name}
            for _rank, name in hits[:MAX_RESULTS]
        ]

    def status(self, name):
        entry = self.entries.get(name) if isinstance(name, str) else None
        if entry is None:
            raise ToolError(UNKNOWN_SKILL)
        found, error = findings.lookup(self.fetcher, entry['name'])
        pin = version.fastmcp_version()
        return {
            'name': entry['name'],
            'version': entry.get('version'),
            'owner': entry.get('owner'),
            'last_verified': entry.get('last_verified'),
            'stale': entry.get('stale') is True,
            'findings': found,
            'findings_error': error,
            'catalog_tag': self.snapshot.tag,
            'catalog_commit': self.snapshot.commit,
            'fastmcp_version': pin,
            'fastmcp_prerelease': version.is_prerelease(pin),
            'notice': version.prerelease_notice(pin),
        }


def register(mcp, snapshot, findings_fetcher=None):
    """Add `search_skills` and `skill_status` to `mcp`.

    `findings_fetcher(name)` returns `[{url, title}]` and may raise; the
    default asks the public GitHub search API (findings.GitHubFindings).
    """
    catalog = Catalog(snapshot, findings_fetcher)

    @mcp.tool
    def search_skills(query: str, scope: str = 'catalog') -> list[dict]:
        """Find catalog skills whose name or description contains a word of `query`.

        Name matches come first, then description matches, each sorted by
        name; at most 20 results, each with `name`, `description` and `uri`.
        `scope` is "catalog"; "third-party" needs the M7 index.
        """
        return catalog.search(query, scope)

    @mcp.tool
    def skill_status(name: str) -> dict:
        """Version, owner, last verified date, `stale` and open freshness findings of a skill.

        Also names the catalog tag and the FastMCP version, with a notice when
        that version is a pre-release. Check this before relying on a skill.
        """
        return catalog.status(name)

    return catalog
