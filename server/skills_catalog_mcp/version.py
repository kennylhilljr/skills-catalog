"""The installed FastMCP version and whether it is a pre-release (MCP-8, DESIGN 4.4)."""

from __future__ import annotations

import importlib.metadata
import re

# PEP 440: release, optional pre-release (a, b, rc and their spellings),
# optional post-release, optional dev release. A local label after `+` is
# ignored.
_PEP440 = re.compile(
    r'^v?\d+(?:\.\d+)*'
    r'(?P<pre>[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?'
    r'(?:-\d+|[-_.]?(?:post|rev|r)[-_.]?\d*)?'
    r'(?P<dev>[-_.]?dev[-_.]?\d*)?$'
)


def fastmcp_version() -> str:
    """The version of the installed `fastmcp` distribution."""
    return importlib.metadata.version('fastmcp')


def is_prerelease(version: str) -> bool:
    """True when `version` has an `a`, `b`, `rc` or `.dev` segment (PEP 440).

    A string that is not a PEP 440 version also counts as a pre-release, so
    the notice errs on the side of being shown.
    """
    text = str(version).strip().lower().split('+', 1)[0]
    match = _PEP440.match(text)
    if match is None:
        return True
    return bool(match.group('pre') or match.group('dev'))


def prerelease_notice(version: str):
    """The MCP-8 notice for a pre-release, else None."""
    if is_prerelease(version):
        return 'FastMCP %s is a pre-release; the skills API may change' % version
    return None
