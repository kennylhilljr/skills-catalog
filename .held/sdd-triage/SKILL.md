---
name: sdd-triage
description: "Classify support work (failures, bugs, spec gaps, blockers and drift), assign a severity from S0 to S4, cite the affected criteria and place it in the queue. Use for incidents, bug reports, broken builds, blocked work or verifier findings."
argument-hint: "<short description of the problem>"
metadata:
  catalog-version: "1.1.0"
  catalog-owner: "sdd_framework"
  catalog-provenance: "own"
  catalog-targets: "claude"
  catalog-consumers: "none"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git,node"
---

# sdd-triage

## Purpose

Give support work the same discipline as delivery work. Every item gets a type, a severity, the criterion IDs it affects and the artifact where the fix is likely to start, which the owner confirms. Severity labels (S0 to S4) are separate from story priority (P1 to P3); this reference profile places both in one combined queue, a local choice.

## When to use

- An incident, outage, failed deploy or degradation.
- A bug report or a failing test.
- Delivery is blocked (failing CI, missing access, an unanswered question, an external dependency).
- `sdd-verify` reports drift.

## Inputs

1. `AGENTS.md`.
2. The report: description, logs, failing tests, affected users.
3. The component PRDs (criteria and invariants), runbooks in `docs/runbooks/`.
4. `docs/trace/index.yaml` and the current milestone plan.

## Steps

1. Classify the item and pick the triage persona:

| Type | What it is | Triaged by | Fix starts at |
| --- | --- | --- | --- |
| Failure | Outage, degradation or failed deploy | `sre` | Runbook first, then the first artifact that lost intent |
| Bug | Behavior violates an existing criterion | `verifier` | Code, with a reproducing test citing the criterion |
| Spec gap | No criterion covers the behavior, or a criterion is wrong | `verifier` | The PRD or LLD; new capability becomes delivery work with a P rank |
| Blocker | Something stops delivery | `planner` | Whoever owns the blocking item |
| Drift | Two artifacts in the trace disagree | `verifier` | The first artifact the owner says no longer reflects approved intent |

2. Dispatch that persona with the Agent tool to find the affected criterion IDs (search for `@spec` and the PRDs).
3. Assign severity from urgency and impact:

| Severity | Urgency | Impact | Example | Response |
| --- | --- | --- | --- | --- |
| S0 | Critical | Extensive | System outage, or any breach of a PRD invariant | Immediate |
| S1 | High | Large | Major feature malfunctioning | Urgent, within the normal schedule |
| S2 | Moderate | Moderate | Minor feature malfunctioning | Ranked against other work |
| S3 | Low | Minor | A feature blocks a few users | Part of routine work |
| S4 | Negligible | Negligible | Cosmetic issue with no effect on users | Backlog |

4. Apply the overrides: a breach of an invariant or a security criterion is S0 or S1 whatever its reach; a blocker takes the severity of the work it blocks (blocking P1 work makes it S1).
5. For S0 and S1, ask the owner to confirm the severity. S0 interrupts work in flight; S1 goes ahead of P1 work at the next task boundary. Both take the fast path: one combined human review, but the trace still applies (a reproducing test citing the criterion, then the fix).
6. Place the item in the queue. Priority and severity are separate lenses; this reference profile tests a combined ordering, S0 > S1 > P1 > S2 = P2 > S3 = P3 > S4, with equal ranks ordered by dependency. A repository that maps both lenses into its own portfolio and incident processes follows those instead. Record it in the issue tracker with type, severity and criterion IDs.
7. For S0 and S1, create a postmortem once the incident is over: `node tools/sdd/cli.mjs new postmortem --name <short-name>`. The `sre` drafts it; the owner approves it.

## Outputs

- A triage record: type, severity, criterion IDs, starting artifact, owner, queue position.
- A postmortem in `docs/postmortems/` for S0 and S1.

## Stop and gate

The owner confirms every S0 and S1 and may override any severity. Agents never close an incident on their own judgment.

## Rules

- The owner names the first artifact in the delivery trace that no longer reflects approved intent; the fix starts there. A bug against an unchanged criterion starts in code, with a reproducing test.
- A bug without a criterion is a spec gap: fix the spec first.
- Never weaken a test to make it pass.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.1.0 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-triage` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.1.0 | 2026-10-22 | Priority and severity are separate lenses; combined queue is a local choice; drift rule names the first artifact that lost intent; "starting layer" becomes the starting artifact |
| 1.0.0 | 2026-10-22 | First release |
