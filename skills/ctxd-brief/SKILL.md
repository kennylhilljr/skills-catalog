---
name: ctxd-brief
description: Pull the current project's context brief, open signals and sync state from ctxd and format them as a session brief. Use when starting a work session in a ctxd-scoped project, or when asked for a ctxd brief or project orientation.
metadata:
  catalog-version: "1.0.0"
  catalog-owner: "kennylhilljr"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "ctxd"
---

Run `ctxd brief` for the current working directory scope and format the output for immediate use.

Steps:
1. Run `ctxd brief` and capture output
2. Run `ctxd status` to get sync state, scope list, and any pending review items
3. Run `ctxd signals` to get open blockers, flags, and incidents

Format the combined output as a session brief:

```
## Session Brief: <project name>, <today's date>

### Context
<ctxd brief output, cleaned up>

### Open signals
<blockers, flags, incidents from ctxd signals>

### Scope status
<sync state, last sync time>
```

If `ctxd brief` returns nothing (new project or unscoped directory), say so and offer to run `ctxd scope` to set one up.

If there are pending review items from `ctxd status`, list them at the top under "Needs review" so they don't get missed.

This skill is read-only. It never modifies ctxd state.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-11-11 | Imported from `~/.claude/skills/ctxd-brief` (local only, no repository; sha256 `f61c91430a3f`); description given a use clause, dashes replaced |
