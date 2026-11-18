# skills-catalog MCP server

A FastMCP server that serves the catalog's skills at one tagged release to interactive agents (DESIGN 4.4, SPEC MCP-1). It reads a git tag, never the working tree.

## Run

From the repository root, with [uv](https://docs.astral.sh/uv/):

```sh
uv run --directory server --frozen python -m skills_catalog_mcp --repo ..
```

`--directory server` makes `server/` the working directory, so `--repo ..` names the repository root. Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo PATH` | the parent of `server/` | the catalog git repository |
| `--ref TAG` | the newest `v*` tag | the tag to serve; a branch or a commit sha is refused |
| `--exclude-file PATH` | `~/.config/skills-catalog/exclude.txt` | the local exclusion list (DESIGN 5.1) |
| `--transport stdio` | `stdio` | the transport |

The server exits with status 2 and `error: no catalog tag` when the repository has no `v*` tag, and `error: ref is not a tag` when `--ref` names anything but a tag.

## Client config (stdio)

Claude Code, in `.mcp.json` or with `claude mcp add`:

```json
{
  "mcpServers": {
    "skills-catalog": {
      "command": "uv",
      "args": ["run", "--directory", "<catalog>/server", "--frozen", "python", "-m", "skills_catalog_mcp", "--repo", ".."]
    }
  }
}
```

OpenClaw, under `mcp.servers` in its config (or the same object with `openclaw mcp set skills-catalog '<json>'`):

```json
{
  "mcp": {
    "servers": {
      "skills-catalog": {
        "command": "uv",
        "args": ["run", "--directory", "<catalog>/server", "--frozen", "python", "-m", "skills_catalog_mcp", "--repo", ".."]
      }
    }
  }
}
```

Replace `<catalog>` with the path of your clone.

## What is served

At start the server extracts `skills/` and `catalog.json` of the tag with `git archive` into a private temp folder (mode 0700), and removes it on exit. It then serves:

- every skill in the tag's `catalog.json` `skills` array as `skill://<name>/SKILL.md`, and each of its other files as `skill://<name>/<path>`, with the bytes of the tag;
- a `skill://<name>/_manifest` resource per skill, and the Skills extension (`skills/list`), both from FastMCP's `SkillsDirectoryProvider`.

It refuses to start when a folder under `skills/` is not in the manifest, a file's sha256 differs from the manifest, or a served skill folder holds a symlink.

## What is not served

- Held skills: `.held/` (the `sdd-*` mirrors and `zz-canary`) and the manifest's `held` array (SPEC MCP-6, INV-8).
- Anything outside `skills/`: `inventory/`, `freshness/`, `docs/`, `tools/`, the root `README.md` and `catalog.json` itself.
- Skills on the local exclusion list (SPEC CAT-20, INV-7). They are dropped before the provider sees the folder. Asking for one gets the same answer as asking for a name that does not exist, and the server logs only `skipped N skill(s) from the local exclusion list`, and only when N is not zero. The server keeps FastMCP's logger at INFO or above, because FastMCP logs every requested URI at DEBUG.
- Commits after the tag, and the working tree.

## FastMCP version

The server pins `fastmcp==4.2.1` exactly in `pyproject.toml` and `uv.lock`. It is a stable release, not a pre-release. The skills API used here is `fastmcp.skills.provider.SkillsDirectoryProvider` on the server and `Client.list_skill_resources()` and `Client.read_skill()` in the tests. The tests run with `-W error::DeprecationWarning`, so a deprecated name fails them.

## Tests

```sh
uv run --directory server --frozen python -B -W error::DeprecationWarning -m unittest discover -s tests -t .
```

`tests/fixtures.py` builds throwaway catalog repositories with a tag (`make_catalog_repo`). The tests never touch the network, the home folder or the real exclusion list.
