"""Catalog tools: search_skills and skill_status (MCP-3, MCP-4, MCP-8, DESIGN 4.4).

Both tools answer only from the snapshot, which holds the served skills of
one tag: held, canary and excluded skills are not in it, so they can never
match a search, and asking for one gets the same `unknown skill` error as a
name that never existed (INV-7, CAT-20). No error echoes a requested name.
"""

from __future__ import annotations

import os
import re

from fastmcp.exceptions import ToolError

from skills_catalog_mcp import findings, version

MAX_RESULTS = 20
UNKNOWN_SKILL = 'unknown skill'
EMPTY_QUERY = 'query is empty'
NO_THIRD_PARTY = 'third-party index is not available'
UNKNOWN_SCOPE = 'unknown scope'
_WORD = re.compile(r'[a-z0-9]+')
_DESCRIPTION = re.compile(r'^description:(?: (.*))?$')


def _words(text):
    return set(_WORD.findall(str(text).lower()))


def _unquote(value):
    """A scalar the way tools/lint.py parse_scalar reads a description string."""
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def parse_description(text):
    """The top-level `description` of SKILL.md text, in the grammar of tools/lint.py.

    That grammar is a subset of YAML that also accepts plain scalars YAML
    rejects (such as a `: ` inside the value), so the server reads it the
    same way, with the same value, instead of with a YAML parser. Missing,
    unreadable or not a string gives ''.
    """
    lines = text.split('\n')
    if not lines or lines[0] != '---':
        return ''
    lines = lines[1:]
    if '---' not in lines:
        return ''
    lines = lines[:lines.index('---')]
    for i, line in enumerate(lines):
        match = _DESCRIPTION.match(line)
        if not match:
            continue
        value = match.group(1) or ''
        indicator = value.strip()
        if indicator in ('>', '>-', '|', '|-'):
            block = []
            for more in lines[i + 1:]:
                if more and not more.startswith('  '):
                    break
                if more.startswith('  '):
                    block.append(more[2:])
                elif not more.strip():
                    block.append('')
            if indicator in ('>', '>-'):
                result = ' '.join(b for b in block if b.strip())
            else:
                result = '\n'.join(block)
            return result + '\n' if indicator in ('>', '|') else result
        return _unquote(value)
    return ''


def _description(root, name):
    """The `description` from a served SKILL.md's frontmatter, or ''."""
    try:
        with open(os.path.join(root, name, 'SKILL.md'), encoding='utf-8') as handle:
            return parse_description(handle.read())
    except (OSError, UnicodeDecodeError):
        return ''


class Catalog:
    """The served skills of one snapshot and the two tool bodies."""

    def __init__(self, snapshot, findings_fetcher=None, excluded=None):
        self.snapshot = snapshot
        if excluded is None:
            excluded = getattr(snapshot, 'excluded', None) or ()
        self.excluded = frozenset(str(n).lower() for n in excluded)
        self.entries = {e['name']: e for e in snapshot.catalog}
        self.descriptions = {name: _description(snapshot.root, name) for name in self.entries}
        self.fetcher = findings_fetcher if findings_fetcher is not None else findings.GitHubFindings()

    def _names_excluded(self, text):
        text = text.lower()
        return any(name in text for name in self.excluded)

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
        # A finding whose title contains an excluded name anywhere (case-
        # insensitive substring, like the local exclusion check) is dropped,
        # so no excluded name reaches a tool result (INV-7).
        found = [f for f in found if not self._names_excluded(f['title'] + ' ' + f['url'])]
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


def register(mcp, snapshot, findings_fetcher=None, excluded=None):
    """Add `search_skills` and `skill_status` to `mcp`.

    `findings_fetcher(name)` returns `[{url, title}]` and may raise; the
    default asks the public GitHub search API (findings.GitHubFindings).
    `excluded` is the local exclusion list (lowercased names); it defaults to
    `snapshot.excluded` when the snapshot carries one. Findings whose title
    names an excluded skill are dropped.
    """
    catalog = Catalog(snapshot, findings_fetcher, excluded)

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
