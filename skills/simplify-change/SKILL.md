---
name: simplify-change
description: Remove unnecessary complexity from the code you changed without changing behavior or tests. Use after the change works and its tests pass, before verify-change and review.
metadata:
  catalog-version: "1.2.0"
  catalog-owner: "ecc-customization"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git"
---

# Simplify a change

1. Read your own diff: `git diff $(git merge-base HEAD <default branch>)...HEAD` plus uncommitted changes.
2. Look for, and remove:
   - code no requirement asks for (speculative options, unused parameters, dead branches);
   - duplication a small helper would remove;
   - wrappers, layers or classes that add no behavior;
   - comments that restate the code; keep the ones that say why.
3. Keep every name the specification owns exactly as written, keep the public behavior, and change no test except to remove a duplicate.
4. Run your acceptance commands and the fast gate named in AGENTS.md again; they must still pass.
5. Commit the simplification separately with a message starting "Simplify:".

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.2.0 | 2026-11-11 | Merged into skills-catalog from ecc-customization `kit/.agents/skills/simplify-change` at `0e73281` and research-mcp `.agents/skills/simplify-change` 1.1.0 at `ef14355`: the kit's fast gate placeholder replaced by a reference to AGENTS.md |
| 1.1.0 | 2026-10-22 | SDD adoption (research-mcp): LLDs own names, tests and code cite criteria with `@spec`, clarifications live in the LLDs |
| 1.0.0 | 2026-10-20 | First release |
