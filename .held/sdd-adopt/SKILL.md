---
name: sdd-adopt
description: "Bring the SDD framework into an existing, running application without breaking it: analyze the repository, add the framework files, list the information people still need to supply, and draft specs for existing behavior. Use for brownfield repositories, adopt SDD, map this codebase, or add the framework to this repo."
argument-hint: "[path to the repository, default .]"
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

# sdd-adopt

## Purpose

Start spec-driven development in a codebase that already works. The adoption must not break anything: no existing file is modified or deleted, no application command is run, and CI checks start in advisory mode. The skill then helps the owner fill in what code cannot reveal, and turns existing behavior into draft acceptance criteria.

## When to use

- The repository has application code but no SDD docs and no `sdd.config.json`.
- The owner asks to add the framework to an existing codebase.

This skill runs before the framework exists in the target repository, so every command uses the framework's own copy of the CLI. In the commands below, `<framework>` is the local clone of the framework repository, or `${CLAUDE_PLUGIN_ROOT}` when the framework is installed as a Claude Code plugin. Ask the owner for the path if neither is known.

For a new, empty repository use `node <framework>/tools/sdd/cli.mjs init <path>` instead. For a repository that already has `sdd.config.json`, use `node <framework>/tools/sdd/cli.mjs upgrade <path>`.

## Inputs

1. The repository's existing `AGENTS.md`, `CLAUDE.md` and README, if any.
2. The output of `node <framework>/tools/sdd/cli.mjs adopt <path>` (dry run).
3. After apply: `docs/adoption/inventory-v1.md` and `docs/adoption/missing-information-v1.md`.

## Steps

1. Check the working tree is clean (`git status`). If not, ask the owner to commit or stash first.
2. Run the dry run: `node <framework>/tools/sdd/cli.mjs adopt <path>`. Show the owner the plan: files to create, proposals in `.sdd-proposed/` for files that already exist, detected components and prefixes, and where the doc map goes (`docs/DOCMAP.md` when `docs/README.md` already exists).
3. Ask the owner to confirm the plan and the component names and prefixes. Only then run `node <framework>/tools/sdd/cli.mjs adopt <path> --apply`. From here on the framework's tools exist in the repository, so the later steps use `node tools/sdd/cli.mjs`.
4. Dispatch the `analyst` persona to read `docs/adoption/inventory-v1.md` and confirm or correct it against the code.
5. Interview the owner for every "Missing" item in `docs/adoption/missing-information-v1.md`, starting with the twelve seed questions, the gate approvers and the organizational scope (fill in `docs/scope/scope-map.yaml` and `project.scope` with the owner). Record answers in the PRD sections the table names; leave anything unanswered as `- [ ] Q:`.
6. Help the owner merge each file in `.sdd-proposed/` into its original by hand (for example adding `@AGENTS.md` to an existing `CLAUDE.md`, or the hooks into an existing `.claude/settings.json`), then delete `.sdd-proposed/`. Never overwrite the original yourself. Show the owner the lint, format and type check exclusions the missing-information file lists (`tools/`, `.sdd-proposed/`); the owner edits those configs.
7. For one component at a time, starting with the one changing soonest, dispatch the `product-writer` persona to draft existing behavior as candidate criteria `<PREFIX>-<n>` tagged `[inferred]` for the owner to select and approve (C), citing the tests that show the behavior. Dispatch the `architect` persona to confirm the SPEC and LLD drafts, keeping `[inferred]` on unconfirmed rationale.
8. Propose `@spec` annotations on existing tests that exercise each draft criterion. Add them only with the owner's agreement; a comment is the only change allowed in existing code.
9. Run `node tools/sdd/cli.mjs check` (advisory) and `node tools/sdd/cli.mjs docmap`. The doc map is written to `paths.docMap`: `docs/DOCMAP.md` when the repository already had a `docs/README.md`, so the existing file is never touched.
10. Agree with the owner when CI should stop being advisory, usually once the first components reach AUDITED in `docs/trace/index.yaml`.

## Outputs

- Framework machinery, draft docs and `sdd.config.json` (with `adoption.mode: advisory`).
- `docs/adoption/inventory-v1.md` and `docs/adoption/missing-information-v1.md`.
- `.github/workflows/sdd.yml` (advisory) when the repository uses GitHub.
- Draft criteria with `[inferred]` tags; `@spec` comments on existing tests where the owner agreed.

## Stop and gate

Stop after the dry run for the owner's go-ahead, and again after the missing-information interview. Draft PRDs follow the normal gates; agents never set `status: approved`.

## Rules

- Never modify or delete an existing file except adding `@spec` comments the owner approved. This includes an existing `docs/README.md`, lint and format configs, and CI files.
- Never run builds, migrations, deployments or the application's own scripts.
- `[inferred]` stays until the owner confirms the content.
- Existing agent instructions are merged by the owner, never replaced.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.1.0 | 2026-11-11 | Imported into skills-catalog as a held mirror of the `catalog-owner` repo's `.claude/skills/sdd-adopt` at `72ce060`; body unchanged, frontmatter moved to the catalog schema |
| 1.1.0 | 2026-10-22 | Interview covers the scope map; criteria from existing behavior are drafted as C; adopt never adds CODEOWNERS |
| 1.0.0 | 2026-10-22 | First release |
