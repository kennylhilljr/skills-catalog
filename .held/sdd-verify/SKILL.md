---
name: sdd-verify
description: "Independently check that tests and code still match the approved spec: run spec-lint and trace-check, regenerate coverage, update the audit index and report drift and annotation reach. Use after implementation, before a merge, or on a schedule."
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

# sdd-verify

## Purpose

Prove the trace holds. Structural checks confirm that every link exists. The audit index records where each component stands and the commit it was last audited at, so the next run re-checks only what changed. When enabled, a bidirectional differential checks that the linked pieces mean the same thing.

## When to use

- After `sdd-implement`, before review or merge.
- Before any gate, as an independent check.
- On a schedule, to catch drift between sessions.

## Inputs

1. `AGENTS.md`.
2. `docs/README.md`, `docs/trace/index.yaml` and `sdd.config.json`.
3. Approved PRDs, SPEC and LLDs for the components in scope.
4. The latest `docs/test/test-plan-vN.md` (its `deferred:` list) and `docs/trace/coverage.md`.
5. The latest handoff in `docs/build/`.

## Steps

1. Dispatch the `verifier` persona with the Agent tool. It must not be the agent that wrote the work under review. Give it the components in scope and these steps.
2. If `features.autoAcceptClaudeRecommendations` is true, run `node tools/sdd/cli.mjs review-auto-accepted` first. List every document that carries `claude_auto_accepted: true` and `human_reviewed` is absent under a **Pending final review** section of the audit report. Include the count in the summary line, for example: "3 auto-accepted documents await human review (run: node tools/sdd/cli.mjs review-auto-accepted)".

   Run `node tools/sdd/cli.mjs check`. Every error is a finding: reverse orphans (an `@spec` that cites an undefined ID), approved criteria without tests in a component that has a milestone plan, a failing linked test, hollow status (`docs/trace/coverage.md` claims evidence no test shows), a deferred P1 criterion. Warnings include criteria with no planned evidence before build, and governing artifacts approved or changed after the last audit.
3. Run `node tools/trace-check/trace-check.mjs --write` (with `--results <JUnit file>` when a test report exists) to regenerate `docs/trace/coverage.md` and the evidence counts and governing artifact versions in `docs/trace/index.yaml`. The test plan is never edited.
4. For a pull request, run `node tools/trace-check/trace-check.mjs --base <base branch>` and report annotation reach for the changed files. A falling number is a review finding.
5. Re-read each adjacent pair for the changed components (PRD and SPEC, SPEC and LLD, LLD and code) and write one line per pair: "consistent" with a reason, or "needs review" with the tension.
6. Update the audit record of each audited component in `docs/trace/index.yaml`: `status` (UNMAPPED, MAPPED, AUDITED, OK, PARTIAL, BROKEN, STALE), `scope` (a key in the scope map), `owner` (the accountable reviewer), `audited` (today), `audited_sha` (`git rev-parse HEAD`), `findings` (one line), `disposition` (accepted, fix required or exception), `drift` (a list of criterion IDs, or null) and `next` (one line).
7. If `features.differentialAudit` is true, run the bidirectional differential per changed criterion:
   - **A direction:** three blind sessions (subagents given only the criterion text and a one-line project description) each write a minimal implementation.
   - **B direction:** three blind sessions receive the annotated code with `@spec` comments, criterion IDs and requirement-echoing names stripped, and tests excluded, and each reconstructs the single EARS criterion the code satisfies.
   - Compare by behavior, not wording. Classify as coherent, B-only drift (code does more than the criterion), A-only drift (the criterion is loose), bidirectional drift, or inconsistent.
   - Write `docs/trace/audits/<component>/<ID>.md` with the date, commit, classification, findings and a recommended fix, naming the artifact it would start at for the owner to confirm.
8. Report findings ranked by severity, with file and line.

## Outputs

- A verification report in the conversation.
- Regenerated `docs/trace/coverage.md` and updated `docs/trace/index.yaml`.
- Audit records in `docs/trace/audits/` when the differential runs.

## Stop and gate

Findings go to the owner. Drift is never auto-repaired. Verification reports the disagreement; the named owner determines the first artifact in the delivery trace that no longer reflects approved intent; the team corrects it, then updates its dependent artifacts.

## Rules

- Never verify your own work.
- A clean pass means no counterexample was found; say where you looked hardest.
- The checks only see annotated code, and a link does not prove meaning; report reach so gaps are visible, and say what the structural checks cannot show.
- Never edit approved documents and never change `status` on them. The verifier writes only under `docs/trace/`.

## Feature flags

| Flag | Default | Effect |
| --- | --- | --- |
| `features.reachReport` | true | Reports annotation reach in trace-check. |
| `features.differentialAudit` | false | Runs the bidirectional differential in step 7. Switching it off changes nothing else. |
| `features.autoAcceptClaudeRecommendations` | false | When true, step 2 surfaces all auto-accepted documents that have not been human-reviewed yet. |

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.1.1 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-verify` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.1.1 | 2026-10-23 | Surfaces pending auto-accepted documents in step 2 when features.autoAcceptClaudeRecommendations is true |
| 1.1.0 | 2026-10-22 | Evidence states, test reports, audit record fields and the drift rule |
| 1.0.0 | 2026-10-22 | First release |
