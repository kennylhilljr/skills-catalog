"""Build the FastMCP server for one snapshot (MCP-1, DESIGN 4.4)."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import DisabledError, NotFoundError
from fastmcp.server.middleware import Middleware
from fastmcp.skills.extension import SkillsExtension
from fastmcp.skills.provider import SkillsDirectoryProvider
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from skills_catalog_mcp import tools

NOT_FOUND = 'Resource not found'
UNKNOWN_SKILL = 'Unknown skill URI'


class NoEchoNotFound(Middleware):
    """Answer every unknown resource with the same fixed error (INV-7, CAT-20).

    FastMCP echoes the requested URI in a not-found error. The server never
    puts a requested name in an error, so a read of an excluded skill and of
    a name that never existed get byte-identical answers.
    """

    async def on_read_resource(self, context, call_next):
        try:
            return await call_next(context)
        except (NotFoundError, DisabledError):
            raise MCPError(code=INVALID_PARAMS, message=NOT_FOUND) from None


class NoEchoSkillsExtension(SkillsExtension):
    """The Skills extension, with a fixed `skills/get` error for an unknown URI (INV-7).

    FastMCP's own handler puts the requested URI in the error message.
    """

    def clone(self):
        return NoEchoSkillsExtension()

    async def _handle_get(self, context, params):
        try:
            return await super()._handle_get(context, params)
        except MCPError as exc:
            if exc.code != INVALID_PARAMS:
                raise
            raise MCPError(code=INVALID_PARAMS, message=UNKNOWN_SKILL) from None


def build_server(snapshot) -> FastMCP:
    """Serve the snapshot's skills as `skill://` resources, plus the catalog tools.

    `snapshot.root` already holds only the delivered, non-excluded skills of
    one tag, so the provider serves exactly those and nothing else.
    """
    mcp = FastMCP('skills-catalog')
    mcp.add_middleware(NoEchoNotFound())
    mcp.add_provider(SkillsDirectoryProvider(snapshot.root))
    mcp.add_extension(NoEchoSkillsExtension())
    tools.register(mcp, snapshot)
    return mcp
