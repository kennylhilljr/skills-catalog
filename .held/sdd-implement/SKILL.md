---
name: sdd-implement
description: "Build one planned task tests first, with @spec annotations that cite the acceptance criteria, then write the handoff. Use when a milestone plan exists and a task is next in the queue."
argument-hint: "<milestone and task, for example M1 task 4>"
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

# sdd-implement

## Purpose

Turn one task into working, traced code. Tests come first and must fail for the expected reason before any code is written. Every test that exercises a criterion, and every behavior entry point in code, carries `@spec <ID>`.

## When to use

- A milestone plan exists in `docs/build/` and the LLD of every component it covers is approved (Gate 3). The gate hook checks the plan you name (`/sdd-implement M1`), or every open plan when none is named.
- A fix on the short path has its criterion and task ready.

## Inputs

1. `AGENTS.md` (commands, definition of done).
2. The task in `docs/build/M<n>-plan-vN.md`.
3. The component PRD (criterion text), the approved LLD and contracts.
4. `docs/test/test-plan-vN.md`.

## Steps

1. Pick the next task in queue order. Read its criterion IDs and the exact EARS text of each.
2. Dispatch the `implementer` persona with the Agent tool. The dispatch prompt must carry, because a subagent does not see this skill:
   - the criterion IDs in scope and their verbatim text;
   - "write failing tests first; do not write code until they fail for the expected reason";
   - "annotate the test that exercises each criterion with `// @spec <ID>` (or the comment syntax of the language), and the entry point of the behavior in code, not every helper";
   - "a criterion that admits two readings comes back as a question, never as a silently chosen reading";
   - the commands from `AGENTS.md` to run tests and type checks.
3. When the implementer returns, set the task's Status to `done` in the plan's task table (a draft plan in place; an approved plan in its next version, `node tools/sdd/cli.mjs new --next <path>`). From then on trace-check requires a test for each criterion the task names. Then run the tests and `node tools/sdd/cli.mjs check` yourself. Fix or send back until both pass.
4. Run `node tools/trace-check/trace-check.mjs --write` to regenerate `docs/trace/coverage.md`. To record passing and failing evidence, produce a JUnit XML report with the project's test runner and add `--results <file>` (do not commit a report from a local run as release evidence).
5. Write or update the handoff: `node tools/sdd/cli.mjs new handoff --milestone M<n>` (first task of the milestone) recording what was built, the criterion IDs, the commands run and their results (which criteria passed, failed, were deferred or excepted), the code review, and anything left open.
6. Hand over to `sdd-verify`. The implementer never verifies its own work.

## Outputs

- Code and tests with `@spec` annotations.
- The task's Status set to `done` in the plan.
- Regenerated `docs/trace/coverage.md` (the test plan is not edited).
- `docs/build/M<n>-handoff-vN.md`.

## Stop and gate

Code review by a person. Verification by the `verifier` persona through `sdd-verify`.

## Rules

- No code without a failing test first.
- Cite acceptance criteria (`TECH-2`), not stories or requirements.
- If the code needs behavior no criterion states, stop and raise a spec gap; do not add untraced behavior.
- If the design is wrong, stop: the owner names the first artifact that lost intent, and the fix starts there.
- Never edit approved documents; never change `status`.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.1.1 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-implement` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.1.1 | 2026-10-22 | Marks the task done in the plan's Status column, which makes its criteria due in trace-check |
| 1.1.0 | 2026-10-22 | Gate checks the named plan and the approved LLD of each component it covers; handoff records test results and review |
| 1.0.0 | 2026-10-22 | First release |
