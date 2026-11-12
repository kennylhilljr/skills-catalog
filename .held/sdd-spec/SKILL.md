---
name: sdd-spec
description: "Turn an approved problem framing into requirements: user stories with priority, functional requirements and EARS acceptance criteria with stable IDs, in the parent and component PRDs. Use after sdd-intake, or when a bug or small change needs its criteria updated."
argument-hint: "[component name]"
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

# sdd-spec

## Purpose

Write the agent-owned and co-authored parts of the PRDs from the owner's answers: user stories, functional requirements, acceptance criteria in EARS, edge cases, assumptions and the glossary. Each component gets its own component PRD with an ID prefix, and every requirement cites its parent.

## When to use

- Section 1 of the parent PRD has passed the intake checkpoint.
- A new component needs a component PRD.
- A bug or small change needs a new or corrected acceptance criterion (the short path of the size router).

## Inputs

1. `AGENTS.md`.
2. `docs/README.md` (doc map).
3. The latest parent PRD `docs/prd/PRD-vN.md`, Section 1 first.
4. Existing component PRDs in `docs/prd/components/`.
5. `docs/templates/PRD-template.md` and `docs/templates/PRD-component-template.md`.
6. `docs/glossary.md`.

## Steps

1. Confirm Section 1 of the parent PRD is approved by the owner (the intake checkpoint). If not, stop and say so.
2. Split the work into components with the owner's help. Each component needs a name and a prefix of 2 to 10 capital letters (for example `tech` and `TECH`). Record them in `sdd.config.json` under `components` and in the parent PRD's component table.
3. Create each component PRD: `node tools/sdd/cli.mjs new prd-component --component <name> --prefix <PREFIX>`.
4. Dispatch the `product-writer` persona with the Agent tool for all components in parallel (one Agent call per component, sent in the same message). Give each instance the component name, prefix, the parent requirement IDs in scope and the rules below. Each writes:
   - user stories `<PREFIX>-US-n` named by the owner, each with a proposed priority P1, P2 or P3 and a one-line reason (the owner decides priority);
   - functional requirements `<PREFIX>-FR-n`, each with a `Parent` column citing `FR-n`;
   - acceptance criteria `<PREFIX>-n` in EARS (or Given/When/Then if the document sets `notation: gherkin`), one response each, with Requirement and Priority columns;
   - NFRs `<PREFIX>-NFR-n` with targets copied from the owner's answers only;
   - invariants `<PREFIX>-INV-n`, each restated as a P1 criterion;
   - edge cases and error behavior, assumptions and open questions, glossary entries.
5. EARS patterns: "The system shall ...", "When <trigger>, the system shall ...", "While <state>, the system shall ...", "If <condition>, then the system shall ...", "Where <feature>, the system shall ...". Name the scope in the condition so a criterion reads correctly when found by search.
6. Run a quick inline consistency check across all component PRDs: every story has at least one criterion; every requirement cites a parent FR; no criterion is untestable as written (vague thresholds, no observable response). Fix mechanical issues in agent-owned sections; add `- [ ] Q:` for anything that needs the owner. If this check passes with no open questions, `sdd-clarify` is optional and the owner may go straight to Gate 1.
7. Run `node tools/sdd/cli.mjs check && node tools/sdd/cli.mjs docmap`.

## Outputs

- `docs/prd/PRD-vN.md` updated with the FR index, NFRs and component table.
- `docs/prd/components/PRD-<component>-vN.md` per component, status `draft`.
- `sdd.config.json` components entry per new component.
- New glossary terms in `docs/glossary.md`.

## Stop and gate

After the inline consistency check, present a Gate 1 checkpoint summary:
- **Components:** list each with story count by priority (P1 / P2 / P3)
- **Invariants and NFRs:** bullet each
- **Open questions:** count and list any `- [ ] Q:` items remaining

Then ask: "Any concerns before I mark the PRDs approved and move to `sdd-hld`?"

If the owner raises a concern or there are open questions, run `sdd-clarify` to resolve them first, then re-present. If none, stamp all PRDs approved (per Auto-accept below) and hand over to `sdd-hld`.

## Rules

- Never write an H section. Stories are named by the owner; priorities are set by the owner.
- IDs are stable: never renumber, never reuse a deleted ID.
- Every criterion states one response, names the system as the subject, and is testable as written.
- Criteria inherit the priority of their story. NFRs and invariants are always P1.
- Anything ambiguous becomes `- [ ] Q:` in the PRD, never a silent choice.
- The PRD says what and why, never how. Design belongs in the SPEC and LLDs.

## Auto-accept

When `features.autoAcceptClaudeRecommendations` is `true` in `sdd.config.json`, set the following fields in the front matter of each PRD this skill writes, instead of `status: draft`:

```yaml
status: approved
claude_auto_accepted: true
claude_auto_accepted_at: <current ISO 8601 UTC timestamp, for example 2026-10-23T14:02Z>
```

When `approvals.requireApprover` is also `true`, additionally set:

```yaml
approved_by: "claude (auto)"
```

The gate hook checks `status` only; it passes on `status: approved` regardless of how it was set. Run `node tools/sdd/cli.mjs review-auto-accepted` to inspect all auto-accepted documents before any release.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.3.0 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-spec` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.3.0 | 2026-10-23 | Lightweight Gate 1 checkpoint: summary + single question before stamping approved |
| 1.2.0 | 2026-10-23 | Parallel component dispatches; inline consistency check; sdd-clarify optional when check is clean; combined check+docmap |
| 1.1.1 | 2026-10-23 | Auto-accept: sets status: approved and stamps audit fields when features.autoAcceptClaudeRecommendations is true |
| 1.1.0 | 2026-10-22 | Intake checkpoint replaces Gate 0; requirements and criteria are drafted as C (owner selects and approves) |
| 1.0.0 | 2026-10-22 | First release |
