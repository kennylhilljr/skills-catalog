---
name: new-mcp
description: Scaffold a new MCP server project in Python or Node.js with its package manifest, a minimal server entry point, README and AGENTS.md, and show how to register it with Claude Code. Use when asked to create, start or scaffold a new MCP server.
metadata:
  catalog-version: "1.0.0"
  catalog-owner: "kennylhilljr"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "python3,node"
---

Scaffold a new MCP server project. Ask the user for:
1. Server name (kebab-case)
2. Language: Python or Node.js (default Python based on existing projects)
3. Brief description of what tools it will expose

Then create the project as `<name>/` in the folder where the user keeps their repositories (ask if unsure; for example `~/Documents/GitHub/<name>/`), with this structure:

**Python (preferred):**
- `pyproject.toml` with name, version, description, `[project.scripts]` entry `<name> = "server:mcp.run"`, deps: `mcp>=1.0`
- `server.py` with a minimal working MCP server using the `mcp` SDK:
  ```python
  from mcp.server.fastmcp import FastMCP
  mcp = FastMCP("<name>")

  @mcp.tool()
  def example_tool(query: str) -> str:
      """Example tool - replace with real implementation."""
      return f"Result for: {query}"

  if __name__ == "__main__":
      mcp.run()
  ```
- `README.md` with setup: `pip install -e .` then the MCP command entry
- `AGENTS.md` describing the server purpose and tool contracts
- `.gitignore` covering Python artifacts

**Node.js:**
- `package.json` with `"type": "module"`, `"bin"` pointing to `src/index.js`, deps: `@modelcontextprotocol/sdk`
- `src/index.js` with minimal MCP server using the official SDK
- `README.md` with setup: `npm install` then `node src/index.js`

After scaffolding, show the user how to wire it into Claude Code:
```bash
claude mcp add <name> --scope user -- <entry-command>
```

Check if `mcp` (Python) or `@modelcontextprotocol/sdk` (Node) is already installed before including in deps.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-11-11 | Imported from `~/.claude/skills/new-mcp` (local only, no repository; sha256 `97c37a67df14`); description given a use clause, target folder made a question |
