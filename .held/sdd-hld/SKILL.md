---
name: sdd-hld
description: "Draft the system high-level design (the SPEC) and ADR options with tradeoffs from approved PRDs. Use after Gate 1, when the requirements are approved and the system shape needs designing or changing."
metadata:
  catalog-version: "1.3.0"
  catalog-owner: "sdd_framework"
  catalog-provenance: "own"
  catalog-targets: "claude"
  catalog-consumers: "none"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git,node"
---

# sdd-hld

## Purpose

Design the shape of the solution. The architect drafts options for every decision with real tradeoffs; the owner chooses. Then the SPEC records the component breakdown, data flow, capacity, risks, trust boundaries, integrations, rollout strategy and the traceability from PRD requirements to SPEC sections.

## When to use

- At least one PRD is approved (Gate 1). The gate hook blocks this skill otherwise.
- A change crosses components or changes the system's shape (the cascade rule sends it here).

## Inputs

1. `AGENTS.md` (approved stack and architecture principles).
2. `docs/README.md`.
3. The approved parent PRD and component PRDs.
4. Existing ADRs in `docs/adr/` and the current SPEC, if any.
5. `docs/templates/SPEC-template.md` and `docs/templates/ADR-template.md`.
6. Research in `docs/research/`.

## Steps

1. Create the SPEC: `node tools/sdd/cli.mjs new spec` (or `new --next docs/spec/SPEC-vN.md` if the current one is approved).
2. **Round 1 — dispatch in parallel:** send the `architect` and `security-reviewer` personas in a single message as two Agent calls.
   - `architect`: lists open decisions from PRD Section 1.9 and the design itself; drafts the initial SPEC structure (component breakdown, data flow and sequence diagrams in Mermaid, capacity estimates from NFR targets, risks and mitigations, PRD-to-SPEC traceability table mapping every FR and criterion ID to a SPEC section).
   - `security-reviewer`: drafts trust boundaries and the threat model (C sections; the owner decides).
3. For each decision where the architect cannot clearly recommend one option, create an ADR: `node tools/sdd/cli.mjs new adr --name <slug>` with status `proposed`, present the options to the owner and record the choice. Skip the ADR when one option clearly dominates — add a one-line note in the SPEC instead.
4. **Round 2 — dispatch in parallel:** send the `sre` and `test-architect` personas in a single message as two Agent calls.
   - `sre`: drafts the rollout and migration strategy from the owner's risk tolerance (PRD 2.9).
   - `test-architect`: drafts the testing strategy section (C: the owner approves it at Gate 2).
5. Cut anything that restates the PRD in different words.
6. Run `node tools/sdd/cli.mjs check && node tools/sdd/cli.mjs docmap`. Approved criteria without tests are not errors yet: tests are required once a milestone plan exists for the component.

## Outputs

- `docs/spec/SPEC-vN.md`, status `draft`.
- `docs/adr/NNNN-<slug>.md`, status `proposed` until the owner decides (only created when the architect flags a real tradeoff).
- Diagrams as Mermaid in the SPEC, optionally rendered under `docs/assets/diagrams/`.

## Stop and gate

After completing the SPEC, present a Gate 2 checkpoint summary:
- **ADRs:** list each open decision with the recommended option and the key tradeoff (or note "no ADR — dominant option chosen")
- **Top risks:** the two or three highest-ranked risks and their mitigations
- **Testing strategy:** one line on the approach
- **Open questions:** any `- [ ] Q:` items remaining

Then ask: "Any concerns, or ADR options to revisit, before I mark the SPEC approved and move to `sdd-lld`?"

If the owner raises a concern or chooses a different ADR option, update the SPEC and ADR, then re-present. If none, stamp the SPEC approved (per Auto-accept below) and hand over to `sdd-lld`.

## Rules

- Never write H content: architecture principles and the approved stack live in `AGENTS.md`.
- Present choices as options with consequences, not as the agent's best guess.
- A decision lives in the lowest artifact (SPEC, ADR or LLD) that covers everything it affects.
- An ADR is never edited after acceptance; a new ADR supersedes it.
- Ambiguity in a PRD goes back to the owner as `- [ ] Q:`; do not design around it silently.

## Auto-accept

When `features.autoAcceptClaudeRecommendations` is `true` in `sdd.config.json`, set the following fields in the front matter of the SPEC this skill writes, instead of `status: draft`:

```yaml
status: approved
claude_auto_accepted: true
claude_auto_accepted_at: <current ISO 8601 UTC timestamp, for example 2026-10-23T14:02Z>
```

When `approvals.requireApprover` is also `true`, additionally set:

```yaml
approved_by: "claude (auto)"
```

ADRs keep `status: proposed`; auto-accept applies to the SPEC only. The gate hook checks `status` only; it passes on `status: approved` regardless of how it was set. Run `node tools/sdd/cli.mjs review-auto-accepted` to inspect all auto-accepted documents before any release.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.3.0 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-hld` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.3.0 | 2026-10-23 | Lightweight Gate 2 checkpoint: summary + single question before stamping approved |
| 1.2.0 | 2026-10-23 | Two parallel dispatch rounds replace four sequential; ADR creation opt-in (skip when one option dominates); combined check+docmap |
| 1.1.1 | 2026-10-23 | Auto-accept: sets status: approved on SPEC and stamps audit fields when features.autoAcceptClaudeRecommendations is true |
| 1.1.0 | 2026-10-22 | Testing strategy drafted as C; check no longer blocks design on missing tests; AGENTS.md is called repository instructions; decisions live in the lowest artifact that covers them |
| 1.0.0 | 2026-10-22 | First release |
