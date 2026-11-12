---
name: verify-change
description: Confirm a change actually works end to end, not only that tests and types pass. Use after the acceptance commands and the fast gate named in AGENTS.md pass, before review or handoff.
metadata:
  catalog-version: "1.2.0"
  catalog-owner: "ecc-customization"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git"
---

# Verify a change

Tests passing is necessary, not sufficient. This skill checks the behavior itself. The project's AGENTS.md names its specification, its fast gate command and its run skill; use those wherever this skill refers to them.

1. **State the claim.** From your unit's specification sections and its acceptance criteria or test ids, write down in one or two sentences what must now be true (for example "a malformed secret reference raises `ValueError` before any external call").
2. **Run the gates.** Your unit's acceptance commands, then the fast gate named in AGENTS.md. Stop here if either fails.
3. **Exercise it directly.** Follow the project's run skill (the `run-<project>` skill AGENTS.md names) and drive the changed behavior the way a caller would (the module API, the tool, the CLI), including at least one input that must be refused. Use fakes and fixtures; the network stays blocked.
4. **Check the clarifications.** If the specification has a clarification for your unit, exercise each case it names.
5. **Record the evidence.** In your handoff's `commands`, list what you ran and what you saw. If something did not behave as the claim says, fix it or report it in `unresolved_failures`; never report success you did not observe.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.2.0 | 2026-11-11 | Merged into skills-catalog from ecc-customization `kit/.agents/skills/verify-change` at `0e73281` and research-mcp `.agents/skills/verify-change` 1.1.0 at `ef14355`: the kit's fast gate and run skill placeholders replaced by references to AGENTS.md |
| 1.1.0 | 2026-10-22 | SDD adoption (research-mcp): LLDs own names, tests and code cite criteria with `@spec`, clarifications live in the LLDs |
| 1.0.0 | 2026-10-20 | First release |
