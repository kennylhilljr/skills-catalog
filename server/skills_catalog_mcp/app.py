"""Build the FastMCP server for one snapshot (MCP-1, DESIGN 4.4)."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.skills.provider import SkillsDirectoryProvider

from skills_catalog_mcp import tools


def build_server(snapshot) -> FastMCP:
    """Serve the snapshot's skills as `skill://` resources, plus the catalog tools.

    `snapshot.root` already holds only the delivered, non-excluded skills of
    one tag, so the provider serves exactly those and nothing else.
    """
    mcp = FastMCP('skills-catalog')
    mcp.add_provider(SkillsDirectoryProvider(snapshot.root))
    tools.register(mcp, snapshot)
    return mcp
