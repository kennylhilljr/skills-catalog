---
name: repo-matrix
description: Build a health matrix of every git repository in a folder, showing last commit, branch, dirty state, open PRs and whether CLAUDE.md and AGENTS.md exist, then list the most actionable items. Use when asked for a repo overview, repo health, which repos have uncommitted work or open PRs, or what to pick up next.
metadata:
  catalog-version: "1.0.0"
  catalog-owner: "kennylhilljr"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git,gh"
---

Scan the folder that holds the user's repositories (ask if unsure; for example `~/Documents/GitHub/`) and produce a health matrix for each repo. Run checks efficiently using Bash.

For each directory that is a git repo, collect:
- **Last commit**: `git log -1 --format="%ar"` (relative date)
- **Dirty**: `git status --porcelain | wc -l` lines (non-zero = uncommitted changes)
- **Branch**: `git branch --show-current`
- **Open PRs**: `gh pr list --json number --jq length` (requires gh auth; skip if not available)
- **Has CLAUDE.md**: check file exists
- **Has AGENTS.md**: check file exists (relevant for agent repos)

Gather data with a single Bash pass using a loop, then format as a compact table:

```
REPO                        LAST COMMIT      BRANCH   DIRTY  PRS  CLAUDE  AGENTS
example-agent               2 hours ago      main     no     0    yes     yes
example-service             3 days ago       main     yes    1    no      yes
example-cli                 1 week ago       main     no     0    yes     yes
example-mcp-server          2 weeks ago      main     no     0    yes     no
```

After the table, surface the three most actionable items:
1. Any repos with open PRs older than 3 days
2. Repos with dirty working state (uncommitted changes)
3. Agent/MCP repos missing AGENTS.md

Limit scan to the 50 most recently modified directories. Skip non-git directories silently.

Sort by most recently committed first so the active work rises to the top.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-11-11 | Imported from `~/.claude/skills/repo-matrix` (local only, no repository; sha256 `4a951dc09d81`); description given a use clause, scan folder made a question, example repo names made generic |
