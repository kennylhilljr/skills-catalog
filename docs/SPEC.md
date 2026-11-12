---
title: "skills-catalog: requirements and acceptance criteria"
type: spec
version: 1
status: draft
owner: "Kenny Hill"
date: 2026-11-11
---

# skills-catalog spec (v1)

Requirements and testable acceptance criteria per milestone, for the design in [DESIGN.md](DESIGN.md). Criteria use EARS patterns: ubiquitous ("The X shall..."), event-driven ("When ..., the X shall..."), state-driven ("While ..."), optional ("Where ...") and unwanted behavior ("If ..., then the X shall..."). Each criterion has a stable ID, a priority (P1 must, P2 should, P3 could), the test level that proves it, and its milestone. IDs are never reused. A changed criterion gets a new ID, and the old one is marked superseded.

Test levels: `unit` (pure function), `integration` (real git, temp folders and fakes for network and CLIs), `e2e` (a real GitHub repository or the real CLIs on the Mac), `manual` (Kenny checks).

## Shared terms

- **Catalog skill**: a folder `skills/<name>/` with a `SKILL.md` on the catalog's `main`.
- **Consumer**: a repo listed in `consumers.json` that has the catalog as a submodule at `.agents/skills/shared`.
- **Finding**: one result of a freshness check, with a kind, a skill (or "catalog"), evidence and a fingerprint used for deduplication.
- **Restricted skill**: a skill in a private repo or a local-only folder. Only its location label, folder name and content hash may be published.
- **Excluded skill**: a skill whose folder name is on the owner's local exclusion list, `~/.config/skills-catalog/exclude.txt` (untracked, never committed; DESIGN 5.1). Nothing about it may be published, not even its name or hash.

## Invariants (all milestones)

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| INV-1 | The catalog shall contain no content from a private repository or a local-only folder other than a location label, a skill folder name and a content hash. | P1 | integration | M1 |
| INV-2 | The catalog shall contain no secret, token, private key, email address, internal hostname or private IP address, and no absolute path or path under a home folder written other than as `~/`. | P1 | integration | M1 |
| INV-3 | The catalog shall contain no third-party skill body except a promoted skill that has a source URL, a 40-hex source sha and an allowlisted license. | P1 | integration | M1 |
| INV-4 | No consumer shall read catalog skills from anything other than its pinned submodule commit. | P1 | integration | M3 |
| INV-5 | No automated job shall push to the catalog's or a consumer's default branch; every change shall arrive as a PR. | P1 | integration | M2 |
| INV-6 | Every scheduled job state shall have a maximum dwell time and an alert when the dwell time is exceeded. | P1 | integration | M2 |
| INV-7 | No tracked file, generated inventory, catalog manifest, MCP response, freshness finding, issue or PR shall contain the name, content or hash of an excluded skill. | P1 | integration | M1 |

## M1: the catalog and its validation CI

**Goal.** The proposed v1 skills are imported, merged and linted, and nothing unsafe can be merged.

**Functional requirements**

- CAT-FR-1: The catalog holds the keep and merge set from `inventory/decisions.json` whose import is not waiting on an open question.
- CAT-FR-2: A linter validates every skill against `schema/frontmatter.schema.json` (DESIGN 4.1).
- CAT-FR-3: A scrub check blocks secrets, PII, internal hosts and home paths in every published file.
- CAT-FR-4: `catalog.json` is generated from `skills/` and checked in CI.
- CAT-FR-5: `tools/inventory.py` regenerates the inventory and keeps the restricted-field rules.

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| CAT-1 | When a PR changes any file, the CI shall run lint, scrub and the catalog manifest check, and the PR shall not be mergeable unless all three pass. | P1 | e2e | M1 |
| CAT-2 | The linter shall reject a `SKILL.md` whose `name` is not 1 to 64 characters of lowercase letters, digits and single hyphens, or does not equal its folder name. | P1 | unit | M1 |
| CAT-3 | The linter shall reject a `description` that is empty, longer than 1,024 characters, or lacking a "Use when", "Use after", "Use before" or "Use for" clause. | P1 | unit | M1 |
| CAT-4 | The linter shall reject a top-level frontmatter key other than `name`, `description`, `license`, `compatibility`, `metadata` and `allowed-tools`, except a Claude Code field on a skill whose `catalog-targets` is exactly `claude`. | P1 | unit | M1 |
| CAT-5 | The linter shall reject a skill whose `metadata` lacks `catalog-version` (semver), `catalog-owner`, `catalog-provenance`, `catalog-targets`, `catalog-last-verified` (ISO date) or `catalog-max-age-days`, or whose metadata values are not strings. | P1 | unit | M1 |
| CAT-6 | If `catalog-provenance` is `promoted`, then the linter shall reject the skill unless `catalog-source` is an https URL, `catalog-source-sha` is 40 hex characters, `catalog-source-license` is on the license allowlist, and a `LICENSE` file is in the skill folder. | P1 | unit | M1 |
| CAT-7 | The linter shall reject a `SKILL.md` over 500 lines, a `{{...}}` placeholder, or a relative file reference that does not resolve inside the skill folder. | P1 | unit | M1 |
| CAT-8 | The scrub shall fail on a match for: provider key and token formats (GitHub, Anthropic, OpenAI, AWS, Slack, private key blocks), high-entropy strings over 32 characters outside fenced examples marked `example`, email addresses outside an allowlist (`noreply@github.com` style), RFC 1918 and link-local IP addresses, hostnames ending in `.local`, `.internal`, `.lan` or `.ts.net`, `/Users/<name>/` and `/home/<name>/` paths, and the names of Kenny's private repos from a configured list. | P1 | unit | M1 |
| CAT-9 | When the scrub fails, it shall print the file, line and kind of each finding and never the matched value. | P1 | unit | M1 |
| CAT-10 | The scrub shall also run as a pre-commit hook installable with `tools/install-hooks`, with the same rules as CI. | P2 | integration | M1 |
| CAT-11 | When `skills/` changes, CI shall regenerate `catalog.json` (name, version, owner, targets, last verified, sha256 of every file) and fail if the committed file differs. | P1 | integration | M1 |
| CAT-12 | The catalog shall hold review-change, verify-change, security-review, simplify-change, cross-review, ears-notation and ecc-operator, each passing CAT-2 to CAT-9. | P1 | integration | M1 |
| CAT-13 | The merged review-change, verify-change, security-review and simplify-change shall name project facts only by reference to the consumer's AGENTS.md (spec location, fast gate command), with no project paths. | P1 | manual | M1 |
| CAT-14 | Each imported skill shall end with a `## Changelog` table whose first row names its source repo path and commit. | P2 | unit | M1 |
| CAT-15 | When `tools/inventory.py` runs, it shall write only location, folder name, content hash, visibility and class for restricted skills, and only counts per source for third-party skills, and shall write no absolute path. | P1 | integration | M1 |
| CAT-16 | The inventory shall mark a skill in an own repo as vendored third-party when its folder has an `ecc-install-state.json` ancestor, or its content hash or description equals a third-party skill's. | P2 | integration | M1 |
| CAT-17 | CI shall run `skills-ref validate` (Agent Skills reference validator) on every skill, pinned to a recorded version. | P2 | integration | M1 |
| CAT-18 | When `tools/inventory.py` runs, it shall read the local exclusion list if present and skip every excluded skill before classification, so that excluded skills are not listed, hashed, clustered, matched to a decision or counted. | P1 | unit | M1 |
| CAT-19 | The local pre-commit and pre-push hook installed by `tools/install-hooks` shall fail if any name on the local exclusion list appears in a tracked file, and shall print the file and line of each finding but never the name. | P1 | unit | M1 |
| CAT-20 | The import tooling, the MCP server (M4) and the freshness bot (M2) shall skip excluded skills: no import, no `skill://` resource, no check, no issue and no PR. | P1 | integration | M1 |

## M2: freshness bot, static checks and upstream watch, with alerting

**Goal.** Every skill is checked nightly and on every PR, upstream changes become catalog work, and nothing stalls without an alert.

**Functional requirements**

- FRS-FR-1: Per-skill static checks (DESIGN 7.2) run on PRs (changed skills) and nightly (all skills).
- FRS-FR-2: Upstream watch (DESIGN 7.1) fetches, normalizes, hashes and diffs each source in `schema/upstream.json`.
- FRS-FR-3: Findings become PRs (mechanical) or issues (judgment), deduplicated by fingerprint.
- FRS-FR-4: Runtime split: Actions for network-only work, launchd on the Mac for CLI-dependent work; heartbeats, dwell limits and the canary.

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| FRS-1 | The nightly Actions job shall run every static check in DESIGN 7.2 that needs no local CLI, over every catalog skill, `externals.json` entry and `zz-canary`. | P1 | e2e | M2 |
| FRS-2 | When a skill's body or scripts mention a model ID that the model table marks deprecated or retired, the check shall produce a finding naming the file, line, ID and replacement. | P1 | unit | M2 |
| FRS-3 | When a skill names a Claude Code tool absent from the current tools reference, or a `claude` or `codex` flag or subcommand absent from the current `--help` output, the check shall produce a finding. | P1 | integration | M2 |
| FRS-4 | While running on the Mac, the job shall run `<cmd> --version` or `<cmd> --help` for each command in `catalog-requires` and each first word of a fenced shell block, and shall produce a finding for each that is missing or exits non-zero. | P1 | integration | M2 |
| FRS-5 | If an http(s) link in a skill returns 4xx or 5xx on two checks at least 20 hours apart, then the check shall produce a dead-link finding; a single failure shall only be recorded. | P1 | integration | M2 |
| FRS-6 | When a relative path a skill references does not exist in the skill folder, the check shall produce a finding. | P1 | unit | M2 |
| FRS-7 | The description check shall produce a finding when a description lacks a use clause, its first sentence exceeds 200 characters, or its lexical trigger-collision score against another catalog skill exceeds the configured threshold. | P2 | unit | M2 |
| FRS-8 | When `catalog-last-verified` is older than `catalog-max-age-days`, the job shall open or update one `re-review` issue for the skill; when the issue has been open 14 days, the job shall set `stale: true` for the skill in `catalog.json` through a PR. | P1 | integration | M2 |
| FRS-9 | `catalog-last-verified` shall be advanced only by the bot, only through a PR, and only when every static check (and from M6, every eval) for the skill passed on that run. | P1 | integration | M2 |
| FRS-10 | For each source in `schema/upstream.json`, the watch shall store a normalized extract and its sha256, and when the sha256 changes, it shall produce a finding holding the diff of the extracts. | P1 | integration | M2 |
| FRS-11 | The watch shall cover the Claude Code skills docs (frontmatter table and load locations), Claude Code releases (npm and changelog), the Claude Code tools reference and plugin format, the Codex skills docs and releases, the Agent Skills spec, the FastMCP skills docs and PyPI releases, and the Anthropic and OpenAI model lists. | P1 | integration | M2 |
| FRS-12 | When an upstream change maps to a rule with a mechanical fix (model ID rename, new optional frontmatter field, moved link with a 301), the bot shall open a PR labeled `freshness` that changes only the affected lines; otherwise it shall open an issue labeled `upstream-change` listing affected skills and the diff. | P1 | integration | M2 |
| FRS-13 | The bot shall not open a second issue or PR for a finding whose fingerprint matches an open one; it shall comment on the open one when the evidence changes. | P1 | unit | M2 |
| FRS-14 | If a watched source fails to fetch on 3 consecutive runs, then the bot shall open a `freshness-alert` issue. | P1 | integration | M2 |
| FRS-15 | While running on the Mac, the job shall record installed `claude` and `codex` versions and produce a finding when either is more than 30 days or one minor version behind the latest release. | P2 | integration | M2 |
| FRS-16 | Each run shall write a heartbeat (start, end, result, counts) to `freshness/state.json` through a PR-less state branch `freshness-state`, and each half (Actions, Mac) shall raise a `freshness-alert` when the other half's last success is older than 26 hours. | P1 | integration | M2 |
| FRS-17 | When any dwell limit in DESIGN 7.6 is exceeded, the bot shall open or update a `freshness-alert` issue and send one OpenClaw ops message per state per day. | P1 | integration | M2 |
| FRS-18 | The nightly run shall report exactly the three seeded `zz-canary` findings (dead link, deprecated model ID, past max age); if it reports fewer, then the run shall be recorded as failed and raise a `freshness-alert`. | P1 | e2e | M2 |
| FRS-19 | The `zz-canary` skill shall be excluded from `catalog.json` and from consumer links. | P1 | unit | M2 |
| FRS-20 | The bot shall authenticate as a GitHub App installed only on `skills-catalog` with contents, pull requests and issues write permissions, and shall hold no other credentials in Actions. | P1 | manual | M2 |
| FRS-21 | The LLM classification step for upstream diffs shall make at most 10 calls per day and fall back to an `upstream-change` issue without classification when the cap is reached. | P2 | unit | M2 |

## M3: submodule rollout to one canary consumer

**Goal.** One bot repo (recommended: `pr-review-bots`, since `cross-review` is its only skill) reads shared skills from a pinned submodule, and iwb and the orchestrator handle it safely.

**Functional requirements**

- SUB-FR-1: The canary consumer adds the submodule and the Claude Code links.
- SUB-FR-2: iwb's two clone paths and the orchestrator's worktree paths initialize the submodule and verify it.
- SUB-FR-3: Protected-path and scope rules cover the submodule; the lessons step routes shared-skill lessons to catalog issues.

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| SUB-1 | The canary consumer shall have a `.gitmodules` entry for `.agents/skills/shared` with URL `https://github.com/kennylhilljr/skills-catalog.git`, pinned to a tagged catalog commit. | P1 | e2e | M3 |
| SUB-2 | The canary consumer shall have one relative symlink `.claude/skills/<name>` to `../../.agents/skills/shared/skills/<name>` per catalog skill, created by `tools/link-consumer`. | P1 | integration | M3 |
| SUB-3 | When `tools/link-consumer --check` runs in a consumer, it shall fail on a missing link, a dangling link, a link to a skill not in the pinned `catalog.json`, or a real folder that shadows a shared skill name. | P1 | integration | M3 |
| SUB-4 | When iwb `clone()` or iwb's work clone clones a consumer, it shall pass `--recurse-submodules --shallow-submodules`. | P1 | integration | M3 |
| SUB-5 | When the orchestrator creates a worktree (preflight or merge queue) in a repo with a `.gitmodules` entry for `.agents/skills/shared`, it shall run `git submodule update --init --depth 1` in the worktree before any agent starts. | P1 | integration | M3 |
| SUB-6 | After every clone or worktree creation, the harness shall run the submodule check (DESIGN 4.2): if the submodule URL is not the allowlisted catalog URL, or `.agents/skills/shared/catalog.json` is missing, or `git submodule status` shows `-` or `+`, then the run shall stop with the reason "shared skills missing" or "submodule URL not allowed". | P1 | integration | M3 |
| SUB-7 | Submodule recursion in iwb and the orchestrator shall run with `protocol.file.allow=never` and accept only `https://github.com/kennylhilljr/skills-catalog.git`. | P1 | integration | M3 |
| SUB-8 | Each iwb and orchestrator run log shall record the pinned catalog commit. | P2 | integration | M3 |
| SUB-9 | iwb's policy for every consumer shall protect `.gitmodules`, `.agents/skills/shared` and the shared `.claude/skills/<name>` links, so a plan whose `scope.files` lists them fails the plan gate and a run that changes them fails the post-run scope check. | P1 | integration | M3 |
| SUB-10 | If `git -C .agents/skills/shared status --porcelain` is non-empty after a unit, then the orchestrator shall fail the unit with the reason "edit inside shared skills". | P1 | integration | M3 |
| SUB-11 | The steward's ownership map shall exclude `.agents/skills/shared/**` and the shared links, and when a lesson's practice resolves to a shared skill, the steward shall record it under "Proposed for the catalog" in the ledger. | P1 | unit | M3 |
| SUB-12 | When a milestone ends with lessons proposed for the catalog, the orchestrator (or iwb on its behalf) shall open one issue per affected skill on `skills-catalog`, labeled `lesson`, with the lesson text and links to the units, and shall make no edit inside the submodule. | P1 | integration | M3 |
| SUB-13 | When the submodule fetch runs with the bot's installation token in the git environment, the clone shall still succeed; if GitHub refuses the token for the catalog, then the fetch shall retry with an empty credential helper. | P1 | e2e | M3 |
| SUB-14 | In the canary consumer, a headless `claude -p` session shall list `cross-review` from the shared submodule, and a `codex exec` session shall list it too. | P1 | e2e | M3 |
| SUB-15 | The orchestrator's gate runner shall keep `submodule` in its refused git subcommands for agents. | P1 | unit | M3 |
| SUB-16 | One end-to-end iwb canary issue on the canary consumer shall complete analyze, plan, code and merge with the submodule in place before a second consumer is converted. | P1 | e2e | M3 |

## M4: the MCP server

**Goal.** Interactive agents can list, read and search catalog skills over MCP.

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| MCP-1 | The server shall serve every catalog skill at a tagged commit as `skill://<name>/SKILL.md` and its other files as `skill://<name>/<path>` resources. | P1 | integration | M4 |
| MCP-2 | Where the client opts into the Skills extension, the server shall answer `skills/list` with each skill's name, description and file manifest digests. | P2 | integration | M4 |
| MCP-3 | The server shall offer `search_skills(query, scope)` over names and descriptions, with `scope` "catalog" by default and "third-party" only when the M7 index exists. | P2 | integration | M4 |
| MCP-4 | The server shall offer `skill_status(name)` returning version, owner, last verified, `stale`, and open freshness finding links. | P2 | integration | M4 |
| MCP-5 | The server shall serve stdio and loopback-only streamable HTTP, and shall refuse to bind to a non-loopback address. | P1 | integration | M4 |
| MCP-6 | The server shall not serve `zz-canary`, `inventory/`, `freshness/` state or any file outside `skills/`. | P1 | integration | M4 |
| MCP-7 | A FastMCP client's `read_skill()` shall verify every file against the served digests, and `sync_skills()` into a temp folder shall produce byte-identical copies of the tag's skill folders. | P1 | integration | M4 |
| MCP-8 | The server shall pin its FastMCP version, and if that version is a pre-release, then `skill_status` and the README shall say so. | P2 | unit | M4 |
| MCP-9 | `tools/install-local` shall install a tagged catalog into `~/.claude/skills` and `~/.agents/skills` as copies, never links, and shall refuse to overwrite a folder it did not install. | P2 | integration | M4 |

## M5: bump-PR automation, and skills-catalog joins the iwb pipeline

**Goal.** Catalog releases reach every consumer as reviewed one-line PRs, and freshness issues are worked by iwb.

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| BMP-1 | When a catalog tag is created, the bump bot shall open or update, for each consumer in `consumers.json`, one PR titled `skills: bump shared to <tag>` that changes only the gitlink and the shared `.claude/skills` links. | P1 | e2e | M5 |
| BMP-2 | The bump PR body shall list each changed skill with old and new version and changelog lines, every skill marked `stale`, and the freshness status of the tagged commit. | P1 | integration | M5 |
| BMP-3 | If a bump PR is already open for a consumer, then the bump bot shall update it to the newest tag instead of opening another. | P1 | integration | M5 |
| BMP-4 | The bump bot shall never merge a bump PR; merging follows the consumer's review rules. | P1 | manual | M5 |
| BMP-5 | If a bump PR stays open 7 days, then the bot shall send an ops message. | P2 | integration | M5 |
| BMP-6 | The remaining consumers (ecc-customization, openclaw-ecc-orchestrator, issue-worker-bots, research-mcp, agent-control-plane-infra) shall be converted one at a time, each after SUB-16 passes on it. | P1 | e2e | M5 |
| BMP-7 | Where a consumer had its own copy of a catalog skill, the conversion PR shall delete the copy or turn it into a project overlay that refers to the shared skill. | P1 | manual | M5 |
| PIP-1 | `kennylhilljr/skills-catalog` shall be added to iwb's repositories and policy, with `.github/workflows/`, `schema/`, `freshness/state.json`, `consumers.json` and `zz-canary` protected. | P1 | e2e | M5 |
| PIP-2 | When the freshness bot opens an issue labeled `freshness`, `lesson` or `upstream-change`, iwb shall pick it up through analyze, plan, cross-review, code and its merge gate. | P1 | e2e | M5 |
| PIP-3 | If an iwb-picked freshness issue makes no stage progress for 3 days, then the freshness bot shall comment on it and send an ops message. | P1 | integration | M5 |
| PIP-4 | One canary freshness issue shall complete the full iwb pipeline before the bot is allowed to open more than 3 issues per day. | P1 | e2e | M5 |

## M6: behavioral evals for claude and codex

**Goal.** Each skill proves it triggers and changes behavior on the current CLIs, and regressions are flagged.

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| EVL-1 | Every catalog skill shall have `evals.json` with at least 3 positive prompts, 3 adjacent negative prompts and 1 execution case with `expectations[]`. | P1 | unit | M6 |
| EVL-2 | The runner shall execute each prompt headless with `claude -p --output-format stream-json` and `codex exec --json` in a temp git repo whose skill folders hold only the catalog, with a pinned model per CLI and a recorded CLI version. | P1 | e2e | M6 |
| EVL-3 | The runner shall count a skill as fired only when the trace shows a Skill tool call for it (Claude Code) or a read of its `SKILL.md` (Codex). | P1 | unit | M6 |
| EVL-4 | The runner shall grade only completed traces (terminal result, no permission denial, every tool call answered), and shall store failed traces without a score. | P1 | unit | M6 |
| EVL-5 | When a skill's positive fire rate drops by more than 25 points against its last passing run, or a negative prompt fires it in more than 1 of 3 runs, or an execution case fails 2 of 3 runs, the bot shall open or update a `freshness` issue with both runs' numbers, models and CLI versions. | P1 | integration | M6 |
| EVL-6 | The runner shall run evals for a skill when its content changes, for every skill when a target CLI's installed version changes, and otherwise on a weekly rotation. | P1 | unit | M6 |
| EVL-7 | The runner shall stop before a case whose estimated cost would exceed the per-run or per-month budget, record the skipped skills, and raise a `freshness-alert` "budget reached". | P1 | integration | M6 |
| EVL-8 | Spend shall be read from the CLIs' JSON usage output and written per run to `freshness/state.json`. | P1 | integration | M6 |
| EVL-9 | Where the installed Claude Code is 2.1.269 or later, the runner shall also run `claude plugin eval` on a generated plugin that wraps the catalog and record the with-and-without delta per case. | P3 | e2e | M6 |
| EVL-10 | The `zz-canary` positive prompt shall fire on both CLIs on every eval run; if it does not, then the run shall fail and raise a `freshness-alert`. | P1 | e2e | M6 |
| EVL-11 | Eval runs shall never use a real consumer checkout, and network access in executor sessions shall be limited to what the case declares. | P1 | integration | M6 |

## M7 (optional): third-party search index

| ID | Criterion | Priority | Test | Milestone |
| --- | --- | --- | --- | --- |
| IDX-1 | The index shall record, for each skill in the watched public repositories, name, description, source repo, path, license and sha, and no skill body. | P2 | integration | M7 |
| IDX-2 | The index shall be read-only: no catalog check, link step or consumer shall read it as a skill source. | P1 | unit | M7 |
| IDX-3 | When a third-party skill is proposed for promotion, the promotion PR shall include provenance (repo, path, sha), the license and its allowlist check, the upstream `LICENSE`, and Kenny's approval before merge. | P1 | manual | M7 |
| IDX-4 | When a promoted skill's upstream changes after its pinned sha, the bot shall open a PR with the upstream diff, the new sha and a license re-check, and shall never auto-merge it. | P1 | integration | M7 |

## Milestone order and gates

| Milestone | Depends on | Gate to start the next |
| --- | --- | --- |
| M1 | none | CAT-1 to CAT-12 pass on main |
| M2 | M1 | 7 consecutive nightly runs with the canary reporting exactly its three findings (FRS-18) |
| M3 | M1 (M2 recommended) | SUB-16 passes on the canary consumer |
| M4 | M1 | MCP-1, MCP-5 to MCP-7 pass; FastMCP decision (DESIGN open question 5) |
| M5 | M2, M3 | PIP-4 passes |
| M6 | M2; host CLIs updated (DESIGN open question 4) | 14 days of nightly evals within budget |
| M7 | M4 | none |
