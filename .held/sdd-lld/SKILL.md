---
name: sdd-lld
description: "Write one low-level design per component from the approved SPEC: contracts, schema, algorithms, errors, configuration, observability and the test plan. Use after Gate 2, when a component is ready for detailed design."
argument-hint: "<component name>"
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

# sdd-lld

## Purpose

Produce the build detail for one component. Agents prepare most of the LLD from the approved SPEC. Decisions that are expensive to reverse are drafted as options for people to decide (C): public contracts, persisted schema, authorization enforcement and the test strategy.

## When to use

- The SPEC is approved (Gate 2) and the component's PRD is approved (Gate 1). The gate hook blocks this skill otherwise; name the component, for example `/sdd-lld tech`.
- A component's design must change within the component (changes that cross components go back to `sdd-hld`).

## Inputs

1. `AGENTS.md`.
2. `docs/README.md`.
3. The approved SPEC and accepted ADRs.
4. The component's approved PRD in `docs/prd/components/`.
5. `docs/templates/LLD-template.md` and `docs/templates/test-plan-template.md`.
6. Existing contracts in `docs/contracts/` and existing code for the component.

## Steps

1. Create the LLD: `node tools/sdd/cli.mjs new lld --component <name>` (or `new --next` if the current one is approved). If multiple components need LLDs in the same session, run this skill for all of them in parallel (one Agent call per component, sent in the same message).
2. Dispatch the `component-designer` persona with the component's criterion IDs and SPEC sections in scope. It writes: API contracts (OpenAPI, JSON Schema or tool schemas in `docs/contracts/`), data model, schema and migrations, module structure and interfaces, algorithms, error handling with retries and idempotency, configuration and feature flags, and a Decisions and alternatives table.
3. After the draft, the component designer runs an edge-case probe on its own gaps: missing state transitions, unstated invariants, unspecified error shapes, ordering assumptions. Gaps go to the owner as `- [ ] Q:` or into the LLD.
4. **Dispatch in parallel:** send `security-reviewer`, `sre`, and `test-architect` as three Agent calls in a single message.
   - `security-reviewer`: authentication and authorization enforcement section (always human-reviewed).
   - `sre`: observability — metrics, logs and alerts with thresholds traced to NFR IDs.
   - `test-architect`: test strategy — map every criterion to planned tests in the LLD's test strategy section and the test plan's planned evidence table (each mapped criterion becomes `planned`), list deferred P2 or P3 criteria in the test plan front matter `deferred:` only with the owner's approval, then run `node tools/trace-check/trace-check.mjs --write` to regenerate `docs/trace/coverage.md`.
5. Run `node tools/sdd/cli.mjs check && node tools/sdd/cli.mjs docmap`. Before build, a criterion without planned evidence is a warning; resolve each one.

## Outputs

- `docs/lld/LLD-<component>-vN.md`, status `draft`.
- New or changed files in `docs/contracts/`.
- An updated draft `docs/test/test-plan-vN.md` (a new version if the current one is approved) and a regenerated `docs/trace/coverage.md`.

## Stop and gate

After completing the LLD, present a Gate 3 checkpoint summary:
- **Public contracts:** list each API or schema file added or changed
- **Authorization:** one line on what is enforced and where
- **Test coverage:** count of criteria mapped to planned tests vs. deferred
- **Open questions:** any `- [ ] Q:` items remaining

Then ask: "Any concerns with the contracts, schema, or test strategy before I mark this LLD approved and move to `sdd-tasks`?"

If the owner raises a concern, address it and re-present. If none, stamp the LLD approved (per Auto-accept below) and hand over to `sdd-tasks`.

## Rules

- Each LLD links back to its SPEC sections and the criterion IDs it satisfies.
- Rationale inferred from existing code is tagged `[inferred]` until the owner confirms it.
- Every optional behavior gets a feature flag that is safe to switch off.
- Anything the LLD cannot settle without the owner becomes `- [ ] Q:`.

## Auto-accept

When `features.autoAcceptClaudeRecommendations` is `true` in `sdd.config.json`, set the following fields in the front matter of the LLD this skill writes, instead of `status: draft`:

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
| 1.3.0 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-lld` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.3.0 | 2026-10-23 | Lightweight Gate 3 checkpoint: summary + single question before stamping approved |
| 1.2.0 | 2026-10-23 | Parallel multi-component dispatch; security+sre+test-architect dispatched in one round; combined check+docmap |
| 1.1.1 | 2026-10-23 | Auto-accept: sets status: approved and stamps audit fields when features.autoAcceptClaudeRecommendations is true |
| 1.1.0 | 2026-10-22 | Gate bound to the named component; contracts, schema, authorization and test strategy drafted as C; planned evidence |
| 1.0.0 | 2026-10-22 | First release |
