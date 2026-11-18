# Tools reference

> Fixture page shaped like the Claude Code tools reference.

| Tool | Description | Permission required |
| :- | :- | :- |
| `Agent` | Spawns a [subagent](https://example.com/sub-agents.md) | No |
| `Bash` | Executes shell commands. See [Bash tool behavior](#bash-tool-behavior) | Yes |
| `Edit` | Makes targeted edits to specific files | Yes |
| `Read` | Reads the contents of files | No |
| `Skill` | Runs a skill within the main conversation | Yes |
| `WebFetch` | Fetches content from a URL | Yes |

## Bash tool behavior

| Setting | Default |
| :- | :- |
| Timeout | 2 minutes |
