---
name: sdd-intake
description: "Interview the owner with the twelve seed questions and turn the answers into Section 1 of the parent PRD. Use when starting a new product, feature or initiative, or when someone asks for a new spec, a new PRD or to capture requirements."
argument-hint: "<one-line idea or problem>"
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

# sdd-intake

## Purpose

Capture what only people can supply: the problem, the outcomes, the scope and what must never happen. Every human-owned (H) section of the PRD maps to one of twelve seed questions. This skill checks for a pre-filled intake form first; if one exists, it reads the answers directly and only asks about gaps. Otherwise it interviews the owner, using analyst-drafted proposals from existing code and docs so the owner confirms rather than composes from blank.

## When to use

- A new product, feature or initiative starts.
- `/sdd-new <idea>` was run (it calls this skill first).
- An existing PRD has Section 1 gaps marked `- [ ] Q:`.

Do not use it for bugs or small changes; those take the short path through `sdd-spec` or `sdd-triage`.

## Inputs

1. `AGENTS.md` (repository instructions: standing rules, approved stack, owners).
2. `docs/README.md` (doc map) to see whether a parent PRD already exists.
3. `docs/standards/spec-driven-development-v3.md` (artifact chain, scope, decision owners, ID scheme).
4. `docs/scope/scope-map.yaml`: the organizational scope (L0 to L3) the work serves. Ask the owner which scope it is if the map does not say.
5. `docs/templates/PRD-template.md`.
6. `docs/intake/intake-form.md` (owner's pre-filled answers, if it exists).
7. The idea passed as the argument, if any.

## Steps

1. Read the inputs. If a parent PRD exists, work on its next draft version instead of a new one (`node tools/sdd/cli.mjs new --next docs/prd/PRD-vN.md` only if the current one is approved; otherwise edit the draft).
2. Dispatch the `analyst` persona with the Agent tool. Give it all twelve seed questions and the full repository context: prior art in `docs/research/`, existing code, existing systems, and answers already settled in `AGENTS.md`. The analyst drafts a proposed answer for every question it can infer from that context. It never invents numbers or dates — those stay blank. It returns a draft answer set, not questions.
3. **If `docs/intake/intake-form.md` exists:** read the owner's answers from it. Merge them with the analyst's draft — the owner's words take precedence. Present only the items that are still blank, conflicting, or marked `- [ ] Q:`, and ask for those as a single consolidated list. Skip the sequential interview entirely.
   **If no intake form exists:** present the analyst's draft answers to the owner for all twelve questions at once, grouped by theme (problem/scope, success/done, constraints/decisions, risk). Ask the owner to confirm, correct or fill in each. One round of questions replaces the sequential one-or-two-at-a-time interview.
   In both paths, skip any question `AGENTS.md` already answers definitively.
4. Ask follow-ups only when an answer is still missing or two answers conflict after the owner's pass. Never fill a gap with a guess.
5. Create the parent PRD if none exists: `node tools/sdd/cli.mjs new prd --title "<product name>"`.
6. Write the answers into Section 1 in the owner's words: problem and why now, users, scope and non-goals, outcomes (`OUT-n` with baseline, target, date), value, leaps of faith, invariants (`INV-n`), constraints, decisions made and open. Record answers to questions 6, 7, 8, 11 and 12 in the Section 2 places they feed, marked as owner input.
7. Every unanswered item becomes an open question in place: `- [ ] Q: <question>`.
8. Run `node tools/sdd/cli.mjs check && node tools/sdd/cli.mjs docmap`.

## Outputs

- `docs/prd/PRD-vN.md` (status `draft`) with Section 1 filled from the owner's answers.
- Open questions listed as `- [ ] Q:` where answers are missing.
- An updated doc map in `docs/README.md`.

## Stop and gate

After writing Section 1, present a short checkpoint summary:
- **Problem:** one sentence
- **Outcomes:** each `OUT-n` with its target and date
- **Invariants:** each `INV-n`
- **Open questions:** count and list any `- [ ] Q:` items

Then ask: "Any concerns before I mark this approved and run `sdd-spec`?"

If the owner raises a concern, address it and re-present. If none, stamp the PRD approved (per Auto-accept below) and hand over to `sdd-spec`.

## Rules

- H sections are the owner's words. You may ask, reformat and tidy; you never invent content.
- Numbers and dates come from people. If the owner gives no target, leave the question open.
- Assign IDs once and never renumber: `OUT-1`, `INV-1`, `FR-1`, `NFR-1` in the parent PRD.
- An answer that admits two readings goes back to the owner as `- [ ] Q:`.
- Project knowledge goes into the PRD, not into agent memory.

## Auto-accept

When `features.autoAcceptClaudeRecommendations` is `true` in `sdd.config.json`, set the following fields in the front matter of the PRD this skill writes, instead of `status: draft`:

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
| 1.3.0 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-intake` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.3.0 | 2026-10-23 | Lightweight checkpoint: summary + single question before stamping approved |
| 1.2.0 | 2026-10-23 | Pre-filled intake form path; analyst drafts all answers before owner review; single-round interview replaces sequential Q&A |
| 1.1.2 | 2026-10-23 | Auto-accept: sets status: approved and stamps audit fields when features.autoAcceptClaudeRecommendations is true |
| 1.1.1 | 2026-10-22 | Links to standard v3 |
| 1.1.0 | 2026-10-22 | Intake checkpoint replaces Gate 0; reads the v2 standard and the scope map; records the scope; AGENTS.md is called repository instructions |
| 1.0.0 | 2026-10-22 | First release |
