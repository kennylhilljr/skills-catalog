---
name: sdd-clarify
description: "Find gaps, conflicts and ambiguous criteria in the PRDs and ask the owner only what is missing, then prepare Gate 1. Use after sdd-spec, before asking the owner to approve the requirements."
argument-hint: "[component name]"
metadata:
  catalog-version: "1.1.1"
  catalog-owner: "sdd_framework"
  catalog-provenance: "own"
  catalog-targets: "claude"
  catalog-consumers: "none"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git,node"
---

# sdd-clarify

## Purpose

Make the PRDs safe to design from. Check that every criterion is testable, that no two criteria contradict each other, that scope is explicit, and that every open question has been put to the owner. Optionally run a blind divergence probe on new criteria. End with a short Gate 1 summary for the owner.

## When to use

- `sdd-spec` has produced or changed PRDs.
- Before Gate 1.
- When a later skill reports that a criterion admits two readings.

## Inputs

1. `AGENTS.md`.
2. The latest parent PRD and component PRDs.
3. `docs/glossary.md`.
4. `sdd.config.json` (feature flags).
5. The output of `node tools/sdd/cli.mjs check`.

## Steps

1. Run `node tools/sdd/cli.mjs check`. Fix spec-lint errors in agent-owned sections; list errors in H sections as questions for the owner.
2. Dispatch the `product-writer` persona to run a consistency pass and return findings, not edits to H sections:
   - **Coverage:** every story has criteria; every requirement cites a parent.
   - **Contradiction:** two criteria that say different things about the same behavior.
   - **Implicit scope:** a criterion that reads as universal but applies to one mode, role or channel.
   - **Testability:** a criterion with no observable response or no measurable threshold.
   - **Cross-component:** two components that both claim the same behavior or state.
3. If `features.divergenceProbe` is true in `sdd.config.json`, dispatch the `verifier` persona as a blind reader for each new or changed criterion. Give it only the criterion text and the glossary, never the conversation. Ask for two or three plausible readings a blind implementer could take. Keep only the readings that would change behavior.
4. Turn every real gap or fork into `- [ ] Q:` in the PRD, then ask the owner, a few questions at a time. Record each answer as a narrower criterion or a new atomic criterion, never a compound "shall X and shall Y".
5. Re-run `node tools/sdd/cli.mjs check` until spec-lint reports no errors.
6. Write the Gate 1 summary for the owner: components, story counts by priority, invariants, NFR targets, deferred items, and anything still open.

## Outputs

- PRDs with resolved questions and narrowed criteria (still `draft`).
- A Gate 1 summary in the conversation.

## Stop and gate

Stop for **Gate 1**. The Product authority, with the feasibility and control reviewers, reviews the PRDs; the approver sets `status: approved` and records `approved_by` and `approved` in each one. Agents never set them unless `features.autoAcceptClaudeRecommendations` is enabled (see Auto-accept below). No approved PRD means `sdd-hld` is blocked by the gate hook.

## Rules

- Ask the owner only what is missing or conflicting. Do not re-ask settled questions.
- A criterion with two readings always goes back to the owner, whatever phase finds it.
- A clean pass from a delegated reader means "no counterexample found", not proof; name where it looked hardest.
- Never edit an approved PRD; use `node tools/sdd/cli.mjs new --next <path>`.

## Feature flags

| Flag | Default | Effect |
| --- | --- | --- |
| `features.divergenceProbe` | false | Runs the blind divergence probe in step 3. Switching it off changes nothing else. |

## Auto-accept

When `features.autoAcceptClaudeRecommendations` is `true` in `sdd.config.json`, set the following fields in the front matter of each PRD this skill finalizes, instead of leaving `status: draft`:

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
| 1.1.1 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-clarify` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.1.1 | 2026-10-23 | Auto-accept: sets status: approved and stamps audit fields when features.autoAcceptClaudeRecommendations is true |
| 1.1.0 | 2026-10-22 | Gate 1 approval records approved_by and approved |
| 1.0.0 | 2026-10-22 | First release |
