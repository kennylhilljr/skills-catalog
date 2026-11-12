---
name: security-review
description: Security review for changes to secrets, redaction, network access, storage, auth, untrusted input or .orchestration. Use for high-risk units and whenever a change handles credentials, untrusted content or network access.
metadata:
  catalog-version: "1.2.0"
  catalog-owner: "ecc-customization"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git"
---

# Security review

Review the unit's diff with an attacker's view. For each item, either show it holds or report a finding (`severity | file:line | issue | fix`). Where the project's AGENTS.md or a project overlay skill names the specification sections, ADRs or high-risk paths behind an item, cite them in the finding.

1. **Secrets**: values never reach argv, logs, exceptions, reprs, fixtures or the handoff; secret store writes pass values on stdin, never on a command line; references are validated before use, so no control character or newline reaches a store command.
2. **Redaction**: every registered secret is masked in every output path, including structured logs and error envelopes.
3. **Network**: listeners bind to loopback unless the specification says otherwise; outbound calls use the project's scoped client; tests cannot reach the network (connect, sendto, sendmsg, DNS lookups).
4. **Untrusted content**: fetched or user-supplied text is marked untrusted and never interpolated into SQL, shell, paths or prompts without validation.
5. **Storage**: parameterized SQL only; migrations cannot be skipped or reordered; file paths are normalized before containment checks.
6. **Auth**: tokens compared in constant time; unauthenticated callers refused on every entry point.
7. **Supply chain**: no dependency outside the stack the specification allows; CI actions pinned by SHA.

Blockers stop the merge. Report what you could not check and why.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.2.0 | 2026-11-11 | Merged into skills-catalog from ecc-customization `kit/.agents/skills/security-review` at `0e73281` and research-mcp `.agents/skills/security-review` 1.1.0 at `ef14355`: the generic checks gain research-mcp's detail; its LLD and ADR citations move to the project's AGENTS.md or overlay skill |
| 1.1.0 | 2026-10-22 | SDD adoption (research-mcp): LLDs own names, tests and code cite criteria with `@spec`, clarifications live in the LLDs |
| 1.0.0 | 2026-10-20 | First release |
