---
name: review-change
description: Review a unit's changes against the specification, its acceptance criteria, the test plan and AGENTS.md, and report findings by severity. Use for the author's self-review before handoff, and when reviewing a PR or verifying a unit.
metadata:
  catalog-version: "1.2.0"
  catalog-owner: "ecc-customization"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git"
---

# Review a change

Review `git diff $(git merge-base HEAD <default branch>)...HEAD` for the unit under review (in an ECC run, the unit named in the branch `ecc/<run>/<unit>`).

The project's AGENTS.md says where its specification lives (for example the newest LLD of the component, or a single specification document), which documents own the names, whether code and tests carry trace annotations, and which checks gate a change. Read it first; every check below refers to it.

## Checklist

1. **Contract.** Every name, signature, field, table, setting and error code matches the specification sections the unit implements. Anything the specification does not mention is not added.
2. **Clarifications.** Every clarification the specification records for this unit holds.
3. **Tests and trace.** Each acceptance criterion or test id the unit owns has a test that checks the behavior it states, including refusals. Where AGENTS.md asks for trace annotations, the test cites the criterion (for example a docstring starting `@spec <ID>:`), the behavior's entry point in code carries `@spec <ID>`, and the trace check AGENTS.md names passes. No test was weakened, skipped or deleted.
4. **Scope.** Only the unit's files changed, and none of the files AGENTS.md says a unit must leave alone.
5. **Rules.** Everything in AGENTS.md "Rules" holds; no secrets in code, logs or fixtures; tests pass with the network blocked.
6. **Simplicity.** Nothing the simplify-change skill would remove.

## Report

One finding per line: `severity | file:line | what is wrong and what the specification or criterion requires`. Severities: `blocker` (violates the specification, a criterion, a test id or a rule), `major` (a likely defect or a missing case), `minor` (clarity). If there are none, say so. When reviewing for ECC, end with the verdict line the prompt specifies.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.2.0 | 2026-11-11 | Merged into skills-catalog from ecc-customization `kit/.agents/skills/review-change` at `0e73281` and research-mcp `.agents/skills/review-change` 1.1.0 at `ef14355`: the trace rules stay, project paths are replaced by references to AGENTS.md |
| 1.1.0 | 2026-10-22 | SDD adoption (research-mcp): LLDs own names, tests and code cite criteria with `@spec`, clarifications live in the LLDs |
| 1.0.0 | 2026-10-20 | First release |
