---
title: "skills-catalog: one source of truth for Kenny's agent skills"
type: design
version: 1
status: draft
owner: "Kenny Hill"
date: 2026-11-11
---

# skills-catalog design (v1)

This repository will hold the de-duplicated set of agent skills that Kenny writes and owns. Every consumer reads from it at a pinned commit, and a freshness bot keeps the skills current as Claude Code, Codex and the skill format change. Requirements and acceptance criteria are in [SPEC.md](SPEC.md). The inventory behind the numbers in this document is in [inventory/own-skills.md](../inventory/own-skills.md).

The repository is public. That is decided.

## 0. Open questions for Kenny

Each question has a recommendation. Work on M1 and M2 can start before any of them is answered.

1. **SDD skills.** The ten `sdd-*` skills are owned by the private `sdd_framework` and installed by its installer. `research-mcp` holds a public copy, and 7 of the 10 differ from the source. Should the catalog carry them? *Recommendation:* not in v1. `sdd_framework` stays their owner, and the catalog lists them as externally owned so the freshness bot can still check the public copies.
2. **Local-only global skills.** Seven skills exist only in `~/.claude/skills` or `~/.codex/skills`: agent-debug, ctxd-brief, new-agent, new-mcp, repo-matrix, current-time-check and profile-photo-crop. Are they all yours, and may they be published once scrubbed? *Recommendation:* yes for the first five. Leave the two claude.ai-synced ones in claude.ai. Skills on the owner's local exclusion list (section 5.1) are out of scope and not counted here.
3. **prd-synch provenance.** `ecc-customization/skills/prd-synch` has frontmatter name `product-requirements` and `mcpmarket-version` metadata, so it probably came from a marketplace. Do you know its source and license? *Recommendation:* treat it as third-party until reviewed.
4. **CLI versions on the Mac.** Claude Code on the host is 2.1.112, while the current npm release is 2.1.322. Codex is 0.158.0-alpha.7, while 0.167.2 is current. Behavioral evals measure whatever router the installed CLI has, and `claude plugin eval` needs 2.1.269 or later. *Recommendation:* update both CLIs (you, through `/update` or a terminal) before M6. The freshness bot reports host CLI lag from M2 on.
5. **FastMCP pre-release.** The skills provider is documented as new in FastMCP 4.2.0, but PyPI has only `4.2.0a1` to `4.2.0a4` (latest stable is 4.1.8). *Recommendation:* build M4 against a pinned alpha behind a flag, and switch to 4.2.0 when it ships. The upstream watch tracks the release.
6. **Eval budget.** *Recommendation:* $5 per nightly run, $40 per month, and at most 3 runs per case. Behavioral evals run only for skills whose content, or whose target CLI version, changed since the last pass.
7. **Alert channel.** *Recommendation:* a GitHub issue labeled `freshness-alert` on this repo, plus an OpenClaw ops message for anything that stalls longer than its dwell limit. No email.
8. **Your own machines.** For interactive use, should `~/.claude/skills` and `~/.agents/skills` read from a local catalog checkout? *Recommendation:* yes, through `tools/install-local` (a copy at a tag, not a link to a working tree), so personal sessions are pinned the same way the bots are.

## 1. What the inventory found

The full tables are in the inventory. These are the headline facts that shaped the design (scan of 2026-11-11, after the local exclusion list in section 5.1 is applied):

| Fact | Number |
| --- | --- |
| `SKILL.md` files under `~/Documents/GitHub` (excluding `node_modules`, `.venv`, `venv`, `site-packages`) | 1,771 (1,520 distinct folder names) |
| In third-party clones | 1,486 (84%) |
| In Kenny's own repos | 285 |
| ...of which vendored third-party copies (ECC in `.kimi-code/`, truecourse and code-review-graph in a generated app) | 86 |
| ...of which second checkouts or `.claude/worktrees` copies of the same repo | 165 |
| ...of which own skills in public repos | 24 |
| ...of which own skills in private repos (`sdd_framework`) | 10 |
| Global installs (`~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`, deduplicated) | 153: 143 third-party (including 5 Codex bundled system skills), 2 probably third-party, 1 own linked to a public repo, 7 own local-only |
| Clusters of own skills | 26, 15 with more than one copy |
| Proposed canonical set (keep or merge) | 12, plus 12 deferred |

Three findings changed the plan:

- **Most of the duplication is not Kenny's.** The expected "skill-creator x6, security-review x6, review-changes, refactor-safely, explore-codebase, debug-issue" copies are almost all third-party: openclaw, awesome-codex-skills, DeepCode, picoclaw and Codex samples for skill-creator; ECC's `.kimi-code` install for security-review and tdd-workflow; and code-review-graph installs for the other four. The real own-skill drift is narrower: the `ecc-customization/kit` templates against their rendered and edited copies in `research-mcp` (4 skills, similarity 0.44 to 0.68), and the `sdd-*` copies.
- **The kit's templating clashes with sharing.** Kit skills carry `{{FAST_GATE_CMD}}` and `{{SLUG}}` placeholders that `ecc-kit` renders per project. A shared skill cannot hold placeholders. Shared skills must therefore name project facts indirectly ("the fast gate named in AGENTS.md"), and project-only skills such as `run-<project>` stay in the kit.
- **Claude Code does not read `.agents/skills`.** Claude Code discovers project skills only at `.claude/skills/<name>/SKILL.md`, one level deep. Codex reads `.agents/skills` and follows symlinks. It also scans subfolders: the Codex skill loader in a January 2026 source checkout walks up to depth 6, and M3 confirms this on the installed CLI (SUB-14). A submodule at `.agents/skills/shared/` is therefore visible to Codex but invisible to Claude Code unless the consumer repo also has `.claude/skills/<name>` entries pointing into it (section 4.3).

## 2. Goals and non-goals

**Goals**

- One owner and one copy for each of Kenny's skills, with a "use when..." description that both Claude Code and Codex route on.
- Bots read skills at a pinned commit, and updates arrive as reviewed one-line bump PRs.
- Interactive agents can browse and fetch skills over MCP.
- Skills stay current: format, tool names, model IDs, CLI flags, links and triggering are checked continuously, and a skill that nobody has re-verified within its max age is forced back into review.

**Non-goals**

- Hosting third-party skills. They stay out. At most there is a read-only search index (M7), and a third-party skill enters the catalog only through promotion (section 9).
- Replacing plugin marketplaces or claude.ai skill sync. The catalog may later publish a Claude Code plugin manifest, but distribution through marketplaces is out of scope for v1.
- Project-specific skills (`run-<project>`, a project's own review overlay). Those live in the project.

## 3. Catalog layout

```text
skills-catalog/
  skills/<name>/SKILL.md           one folder per skill; folder name == frontmatter name
  skills/<name>/references/ scripts/ assets/   optional, per the Agent Skills spec
  skills/<name>/evals.json         behavioral and trigger cases (M6)
  externals.json                   skills owned elsewhere that the bot still checks (sdd-*, promoted third-party pins)
  catalog.json                     generated manifest: name, version, sha256 of every file, targets, last_verified
  schema/frontmatter.schema.json   the catalog's frontmatter contract (section 4.1)
  schema/upstream.json             the watched upstream sources and their last-seen fingerprints (section 7.1)
  freshness/                       the freshness bot (checks, upstream watch, eval runner, reporters)
  server/                          the FastMCP server (M4)
  tools/inventory.py               this inventory; tools/lint, tools/scrub, tools/link-consumer, tools/install-local
  inventory/                       inventory output and decisions
  docs/                            DESIGN.md, SPEC.md
```

Skills sit flat under `skills/`, with no category folders. Codex scans nested folders, but a flat tree keeps the Claude Code link step (4.3) trivial and makes name clashes impossible.

### 3.1 The proposed v1 set

| Skill | Decision | Owner | From |
| --- | --- | --- | --- |
| review-change | merge | ecc-customization | kit template + research-mcp 1.1.0 |
| verify-change | merge | ecc-customization | kit template + research-mcp 1.1.0 |
| security-review | merge | ecc-customization | kit template + research-mcp 1.1.0 |
| simplify-change | merge | ecc-customization | kit template + research-mcp 1.1.0 |
| cross-review | keep | pr-review-bots | pr-review-bots/skills |
| ears-notation | keep (fix dead `scripts/ears-validator.js` reference) | ecc-customization | ecc-customization/skills |
| ecc-operator | keep | ecc-customization | ecc-customization/skills |
| agent-debug, ctxd-brief, new-agent, new-mcp, repo-matrix | keep, after open question 2 | kennylhilljr | local only |

Dropped: `run-project` (stays a kit template) and `profile-photo-crop` (stays in claude.ai). Deferred: the 10 `sdd-*` skills, `product-requirements`, and one claude.ai plugin skill (`current-time-check`). The reasons for each are in the inventory.

## 4. Architecture

```text
             upstream docs, changelogs, releases, model lists
                              |
                     [freshness bot] --- issues / PRs ---> skills-catalog (main, tags)
                              |                                   |
                       evals on the Mac                +----------+-----------+
                                                       |                      |
                                         bump PR per consumer          [MCP server]
                                         (.agents/skills/shared)    skill:// resources
                                                       |            skills/list (exp.)
                                    iwb / orchestrator fresh clones        |
                                    (--recurse-submodules)        OpenClaw, Claude Code
                                                                  interactive sessions
```

### 4.1 Frontmatter schema

Top-level keys use only the six fields of the [Agent Skills spec](https://agentskills.io/specification): `name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`. All six load unchanged in Claude Code, Codex and claude.ai uploads, and the Skills API rejects any other top-level key. The catalog's own fields therefore live under `metadata`, which the spec defines as a map of string keys to string values. Claude Code ignores `metadata`, and Codex does not act on it.

```yaml
---
name: verify-change
description: >-
  Confirm a change actually works end to end, not only that tests and types pass.
  Use after the acceptance commands and the fast gate in AGENTS.md pass, before
  review or handoff, including when the change looks too small to need it.
license: MIT
compatibility: Claude Code 2.1.300+ or Codex CLI 0.160+; git
metadata:
  catalog-version: "1.2.0"          # semver of this skill (section 10)
  catalog-owner: "ecc-customization" # exactly one owner: a repo of Kenny's or "kennylhilljr"
  catalog-provenance: "own"         # own | promoted
  catalog-source: ""                # promoted only: https URL of the upstream repo
  catalog-source-sha: ""            # promoted only: 40-hex commit the copy was taken from
  catalog-source-license: ""        # promoted only: SPDX id, checked on promotion
  catalog-targets: "claude,codex"   # where it must load and pass evals
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "git,gh"        # commands the skill runs; the bot checks they exist (7.2)
---
```

Rules the linter enforces (SPEC M1):

- `name` is 1 to 64 characters of `a-z`, `0-9` and single hyphens, and matches the folder name. This is stricter than Claude Code, which also takes the folder name, and matches the Agent Skills spec, which Codex follows.
- `description` is 1 to 1,024 characters (the spec limit), and front-loads the use case. Codex shortens descriptions when the skill list exceeds 2% of context, and Claude Code truncates `description` plus `when_to_use` at 1,536 characters. It must contain a "Use when", "Use after", "Use before" or "Use for" clause. `when_to_use` is not used, because it is a Claude Code-only field.
- Claude Code-only fields (`argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`, `model`, `effort`, `context`, `agent`, `paths`, `hooks`, `shell`, `disallowed-tools`) are allowed only when `catalog-targets` is `claude`. Otherwise they belong in an overlay (4.3).
- `SKILL.md` stays under 500 lines, and file references are relative and one level deep (spec guidance).
- No placeholders such as `{{...}}`, no absolute paths, no home paths, and no secrets (M1 scrub).

### 4.2 Consumption by bot repos: a git submodule

The six bot repos are `ecc-customization`, `openclaw-ecc-orchestrator`, `issue-worker-bots`, `pr-review-bots`, `research-mcp` and `agent-control-plane-infra`. Each adds the catalog as a submodule:

```ini
[submodule ".agents/skills/shared"]
    path = .agents/skills/shared
    url = https://github.com/kennylhilljr/skills-catalog.git
    branch = main          # informational only; the gitlink commit is what pins it
    shallow = true
```

Rules:

- Use a plain `https://` URL, never SSH or a token in the URL. The catalog is public, so no credential is needed, and bot clones work without keys.
- The gitlink pins the version. A consumer changes it only through a bump PR (section 8).
- `.gitmodules` and the gitlink path `.agents/skills/shared` are protected paths in every consumer (section 4.5).
- The submodule tree is read-only for agents. They never edit inside it, and the lessons step writes elsewhere (section 6).

**Clone and worktree changes (M3).** If a clone skips submodules, the folder exists but is empty, and agents silently run without the shared skills. Every place that materializes a consumer repo must recurse:

| Where | Today | Change |
| --- | --- | --- |
| iwb `clone()` (analysis clone) | `git clone -q --depth 1 --branch <b> https://github.com/<repo>.git` | add `--recurse-submodules --shallow-submodules`, then run the submodule check below |
| iwb work clone (the orchestrator target) | `git clone -q --single-branch --branch <b> ...` | add `--recurse-submodules --shallow-submodules` and the check |
| iwb `trusted_copy()` | fetch with `--no-recurse-submodules` | no change: the result branch carries only the gitlink, which is what should be pushed |
| orchestrator `cli/preflight.py` and `merge_queue/queue.py` `git worktree add` | worktree without submodules | after `worktree add`, run `git -C <wt> submodule update --init --depth 1` (submodule objects are shared through the main clone's `.git/modules`), then the check |
| orchestrator gate runner | `submodule` is in `GIT_REFUSED_SUBCOMMANDS` | keep it refused for agents; only the harness initializes submodules |

**The submodule check.** This runs after every clone or worktree creation, in the harness rather than the agent:

1. `.gitmodules` either has no `.agents/skills/shared` entry, or its URL is exactly `https://github.com/kennylhilljr/skills-catalog.git`. Any other URL fails the run, so a PR cannot repoint the submodule at another repository.
2. Recursion uses `-c protocol.file.allow=never -c submodule.recurse=false`, plus a URL allowlist, so a malicious `.gitmodules` cannot pull local or `ext::` URLs.
3. If the entry exists, `.agents/skills/shared/catalog.json` exists and `git submodule status` shows no `-` (uninitialized) or `+` (wrong commit) prefix. Otherwise the run stops with the reason "shared skills missing". It does not continue silently.
4. The run log records the pinned catalog commit, so a finding can be traced to the skill versions the agent saw.

Token note: the analysis and work clones run with a bot installation token in the git environment. Fetching the public catalog with that token's auth header must work. If GitHub refuses it for a repository outside the installation, the submodule fetch runs with an empty credential helper. M3 tests both cases.

### 4.3 Making Claude Code see the submodule

Codex finds `.agents/skills/shared/<name>/SKILL.md` on its own. Claude Code needs `.claude/skills/<name>`. `tools/link-consumer` (shipped in the catalog, run by the bump PR automation) writes one **relative, in-repo** symlink per shared skill:

```text
.claude/skills/verify-change -> ../../.agents/skills/shared/skills/verify-change
```

This is not the rejected symlink design (section 11). The rejected design pointed outside the repository, at a live working tree. These links stay inside the repository, resolve into the pinned submodule, and change only in the same bump PR that moves the gitlink. Claude Code and Codex both follow symlinked skill folders. On a fresh clone without submodules the links dangle. The submodule check catches that, and the consumer's CI runs `tools/link-consumer --check`, which fails on a missing, dangling or extra link.

A project that needs a project-specific variant (for example the research-mcp security-review citations) keeps a real folder `.claude/skills/security-review-research-mcp/` and `.agents/skills/security-review-research-mcp/` that says "follow the shared security-review skill, plus these project rules". Shared skill names are reserved in consumers: `link-consumer --check` fails when a real folder shadows a shared name.

### 4.4 Interactive agents: the MCP server

`server/` runs a FastMCP server (M4) over the same `skills/` tree, at a tagged release:

- `SkillsDirectoryProvider` (or `SkillCatalogProvider` with `load_skills()` reading the checked-out tag) serves every skill as `skill://<name>/SKILL.md` plus its files. Any MCP client can read these as ordinary resources.
- With `experimental_skills=True`, clients that speak the [Skills extension (SEP-2640)](https://modelcontextprotocol.io/seps/2640-skills-extension) can call `skills/list` and get each skill's description and a hashed file manifest. Few hosts speak it yet, and FastMCP marks it experimental.
- Tools: `search_skills(query)` (name and description match, catalog only) and `skill_status(name)` (version, last verified, open freshness findings), so an agent can see that a skill is stale before using it.
- Transport: stdio for local clients, and streamable HTTP on loopback for OpenClaw. No remote exposure.
- `sync_skills()` from a FastMCP client can install the served skills into `~/.claude/skills` or `~/.agents/skills` on a machine. That is the path `tools/install-local` wraps (open question 8).

Limits, stated plainly: the extension is experimental, and FastMCP's skills support is in pre-release (4.2.0a4 at the time of writing). Claude Code and Codex read skills from disk, so for them the server is a browsing and installing aid, not the delivery path. Bots never depend on it (section 11).

### 4.5 Protected paths and scope in consumers

iwb's policy (`path_rules`) and the orchestrator's unit scopes must treat the submodule as follows:

- `.gitmodules`, `.agents/skills/shared` (the gitlink) and `.claude/skills/<shared-name>` links are **protected** in every consumer's iwb policy (`deny_paths`). A unit plan that lists them in `scope.files` fails the plan gate, and a run that touches them fails the post-run scope check. A bump PR is the only way they change, and it comes from the bump bot, not from an iwb unit.
- Paths under `.agents/skills/shared/` never appear in a diff: git reports only the gitlink. An agent that edits files inside the submodule produces no diff in the parent, so the edit is lost silently. Because of this, the orchestrator's post-unit check runs `git -C .agents/skills/shared status --porcelain` and fails the unit if it is dirty. A dirty submodule means an agent wrote into shared content.
- The bump PR has its own merge rule: it changes only the protected paths above, so iwb's merge gate routes it to Kenny ("the PR changes protected paths"), unless Kenny later allowlists the bump bot for that exact file set (M5).

## 5. Security and trust model

| Asset or risk | Control |
| --- | --- |
| Publishing private content | M1 CI scrub: secrets, tokens, emails, internal hostnames and IPs, absolute and home paths, and a denylist of private repo names in skill bodies. `tools/inventory.py` emits only name and hash for private and local-only skills. |
| A skill that tells an agent to do harm (prompt injection in a skill body) | Every catalog change is a PR, reviewed through cross-review and by Kenny for new skills. Promoted third-party skills are copied and pinned, never fetched live. Skill scripts are reviewed like code. |
| Repointing the submodule | URL allowlist in the submodule check (4.2); `.gitmodules` is a protected path. |
| Drift mid-run | Gitlink pin; bots never read the catalog live. |
| The freshness bot's credentials | A GitHub App with `contents:write`, `pull_requests:write` and `issues:write` on `skills-catalog` only. CLI credentials for evals stay in the Mac's Keychain and are never in Actions. |
| Eval runs touching real repos | Evals run in throwaway temp repos with fixtures, network-limited, with an explicit permission mode (the agent-skills pattern). |
| Unreviewed third-party content | Kept out. Promotion needs provenance, an SPDX license compatible with publishing, and a pinned source sha (section 9). |
| Skills the owner keeps private, by name | Local exclusion list (5.1). Excluded skills never reach the catalog, and the local scrub fails if any excluded name appears in a tracked file. |

### 5.1 Local exclusion list

Some skills on the owner's machines must not be published in any form: not their content, and not their names, hashes or decisions. The owner keeps their folder names in an untracked file outside the repository, `~/.config/skills-catalog/exclude.txt` (one folder name per line, `#` starts a comment). The list itself is never committed, quoted in a PR or written to a log.

- `tools/inventory.py` reads the list and skips matching skills entirely, wherever they are found: they are not listed, hashed, clustered, decided or counted.
- `inventory/decisions.json` never has a key for an excluded skill, and the import tooling, the MCP server and the freshness bot never read, serve, check or open issues about one.
- A local pre-commit and pre-push hook reads the list and fails if any listed name appears in a tracked file, printing the file and line but not the name. CI cannot run this check, because the list never leaves the owner's machine; it adds to the CI scrub and does not replace it.
- Adding a name to the list does not remove it from git history. If a name was published earlier, rewriting history is the owner's decision.

## 6. Lessons write-back

The orchestrator's steward (`team/personas.py` `steward_prompt`) turns persona lessons into edits to "practice files", and lessons often name `.agents/skills/<skill>` as the practice. With a submodule, those edits would land inside the pinned checkout and vanish, or worse, be committed in a detached submodule HEAD.

New rule:

1. The ownership map that the steward receives excludes `.agents/skills/shared/**` and the `.claude/skills/<shared-name>` links. The steward may not edit them.
2. A lesson whose `practice` resolves to a shared skill goes under "Proposed for the catalog" in the ledger, with the skill name, the lesson text and the unit it came from.
3. After the milestone, the orchestrator (or iwb on its behalf) opens **one** issue on `skills-catalog` per affected skill, labeled `lesson`, holding the lessons verbatim and links back to the units. It never opens a direct edit. iwb's pipeline picks the issue up once skills-catalog joins it (M5), and the change reaches consumers through the next bump PR.
4. Project-specific lessons go to the project's overlay skill or AGENTS.md as today.

## 7. The freshness bot

"Dated skills get ignored by agents." The agent-skills eval notes show it directly: the same test-driven-development description fired 6 of 6 runs on Claude Code 2.1.283 and 1 of 6 on 2.1.312 with no change to the skill. The router under it changed. Freshness is therefore a first-class component with its own state, budget and alerts, not a lint step.

### 7.1 Upstream watch

The watched sources, in `schema/upstream.json`:

| Source | What is watched | How |
| --- | --- | --- |
| Claude Code skills docs | frontmatter field table, discovery locations, naming rules, limits (1,536-character listing cap) | fetch `code.claude.com/docs/en/skills` (markdown), extract the frontmatter table and the "Choose where skills load" table, hash each section |
| Claude Code releases | changelog entries mentioning skills, plugins, tools, slash commands or flags; latest version | npm `@anthropic-ai/claude-code` version, and the GitHub releases or CHANGELOG of `anthropics/claude-code` |
| Claude Code tool names | the tools reference table | fetch the tools reference doc and extract tool names |
| Claude Code plugin and marketplace format | `plugin.json` and marketplace schema pages | fetch and hash the schema sections |
| Codex CLI | skills docs (locations, `agents/openai.yaml`, `skills.max_context_tokens`, `[[skills.config]]`), AGENTS.md docs, releases | fetch the Codex skills page; GitHub releases of `openai/codex`; npm `@openai/codex` |
| Agent Skills spec | frontmatter constraints | fetch `agentskills.io/specification`; watch the `agentskills/agentskills` repo, whose `skills-ref validate` is also run as a check |
| FastMCP | skills docs page; PyPI releases (watch for 4.2.0 final) | fetch the doc; PyPI JSON |
| Model IDs | current and deprecated model IDs for Anthropic and OpenAI | fetch the Anthropic models overview and deprecations pages, and the OpenAI models page; keep a table of `{id, status, replacement}` |
| Host CLIs | installed `claude --version` and `codex --version` against the latest release | local, on the Mac |

Each check stores a normalized extract and its hash. On a hash change, the bot:

1. diffs the old and new extracts;
2. classifies the change with a small, rules-first classifier (new or removed frontmatter field, renamed tool, deprecated model, changed location, changed limit), falling back to one LLM call that maps the diff to "affects these skills: ..." with a short reason;
3. writes a finding: a PR when the fix is mechanical (for example rename a deprecated model ID in two skills, or update `schema/frontmatter.schema.json` with a new optional field), otherwise an issue tagged `upstream-change` that lists the affected skills and the diff.

A watched source that fails to fetch 3 times in a row raises an alert. Silence is not "no change".

### 7.2 Per-skill checks

These run nightly over every skill and externally owned entry, and on every catalog PR over the changed skills:

| Check | Fails when |
| --- | --- |
| Frontmatter, Claude Code | a top-level key outside Claude Code's current field table; a YAML parse error; a description above the listing cap |
| Frontmatter, Codex | `name` or `description` missing; a name that breaks the Agent Skills spec; anything `skills-ref validate` rejects; an `agents/openai.yaml` that fails its documented shape |
| Catalog metadata | a missing `catalog-*` key, an unknown owner, `promoted` without source, sha and license |
| Deprecated references | the body or scripts mention a model ID marked deprecated or retired, a tool name not in the current tools reference, or a Claude Code or Codex flag or subcommand not in the current `--help` output |
| Commands exist | each command in `catalog-requires`, and each first word of a fenced shell block, is on PATH on the Mac (`<cli> --version` or `--help` exits 0); subcommands named in the body exist in `<cli> --help` |
| Links | an http(s) link returns 4xx or 5xx twice, 24 hours apart (soft fail first, to avoid flapping) |
| Paths | a relative path the skill references (`references/x.md`, `scripts/y.js`) is missing; for example ears-notation's `scripts/ears-validator.js` |
| Description quality | no "Use when/after/before/for" clause; first sentence longer than 200 characters; a lexical trigger-collision score above the threshold against any other catalog skill (agent-skills Tier 2, stemmed TF-IDF); missing vocabulary from the skill's own positive eval prompts |
| Age | `catalog-last-verified` is older than `catalog-max-age-days` (default 90; scaffolding and API skills 45) |

`catalog-last-verified` moves forward only when every check passes and, from M6 on, the skill's evals pass on the current CLIs. A skill past its max age gets an issue labeled `re-review`, and after 14 more days it is marked `stale` in `catalog.json`. The MCP server's `skill_status` then shows it, and the next bump PR lists it.

### 7.3 Behavioral evals

The pattern follows addyosmani/agent-skills `evals/` (MIT), adapted to two CLIs:

- `skills/<name>/evals.json` has positive prompts (the skill must fire), adjacent negative prompts (it must not), and one or two execution cases with `expectations[]` graded from the transcript.
- **Trigger tier:** each prompt runs headless (`claude -p --output-format stream-json`, `codex exec --json`) in a temp repo whose `.claude/skills` and `.agents/skills` hold only the catalog. "Fired" means the trace shows the Skill tool call (Claude Code) or a read of the skill's `SKILL.md` (Codex). Each case runs with a pinned model and records the CLI version.
- **Behavior tier:** execution cases run with a fixed permission mode in a throwaway git repo, and an LLM grader scores `expectations[]` against the trace. Only completed traces are graded.
- **Regression rule:** a skill is flagged when its fire rate on positive prompts drops by more than 25 points against its last pass, when any negative prompt fires it more than once in 3 runs, or when behavior expectations fail on 2 of 3 runs. The finding names the CLI versions on both sides, since a router change shows up as exactly this.
- For Claude Code 2.1.269 or later, `claude plugin eval` over a generated plugin wrapping the catalog gives a with-and-without delta per case as a second signal.

### 7.4 Promoted third-party skills

For each entry with `catalog-provenance: promoted`, the bot fetches the upstream repository at its default branch, compares the skill folder at `catalog-source-sha` with HEAD, and, when it changed, opens a PR with the upstream diff, the new sha, and a license re-check. It never auto-merges. A changed license, or an upstream that disappeared, becomes an issue labeled `provenance`.

### 7.5 Output

| Finding | Becomes |
| --- | --- |
| Mechanical: model ID rename, dead path fixed by a known move, new optional schema field, link moved with a 301, `last-verified` bump after a clean run | a PR from the freshness bot, labeled `freshness`, small and single-purpose, through the normal cross-review |
| Needs judgment: description rewrite, skill stopped triggering, behavior regression, upstream format change | an issue labeled `freshness` with evidence (diff, eval numbers, CLI versions), written as an iwb-ready task with acceptance criteria |
| Runtime fault: watch source failing, eval harness broken, budget hit | an alert issue labeled `freshness-alert`, plus an ops message |

iwb's analyze, plan, cross-review, code and auto-merge pipeline picks up the issues once skills-catalog joins its repos (M5). Merged changes reach consumers through bump PRs.

### 7.6 Runtime: both, split by what needs the Mac

| Part | Runs on | Why |
| --- | --- | --- |
| PR checks (lint, scrub, schema, paths, links, description quality) | GitHub Actions on every PR | deterministic, no credentials, must gate merges |
| Nightly static checks and upstream watch | GitHub Actions, scheduled daily at 06:00 ET | network fetches and diffs need no local CLIs |
| CLI-dependent checks (commands exist, `--help` flags, host CLI lag) and behavioral evals | launchd on the Mac (`com.kennylhilljr.skills-freshness`), like iwb | needs the installed `claude` and `codex` CLIs and their logged-in credentials, which must not be copied into Actions |

The Mac job writes its results to a `freshness/state.json` and opens PRs and issues through the same GitHub App as Actions. Each half also reports the other's last heartbeat.

**Dwell limits, alerts and the canary.** Every state has a maximum dwell time and an alert:

| State | Max dwell | Alert |
| --- | --- | --- |
| Nightly Actions run not completed | 26 h since the last success | issue + ops message |
| Mac launchd job not completed | 26 h since the last success (Mac asleep counts) | issue + ops message |
| A watched source failing to fetch | 3 consecutive runs | issue |
| Freshness PR open without review | 7 days | ping on the PR, then an issue |
| Freshness issue not picked up by iwb (after M5) | 3 days | issue comment + ops message |
| Skill past max age | 14 days in `re-review` | marked `stale` + ops message |
| Bump PR open in a consumer | 7 days | ops message |

The canary: `skills/zz-canary/` is a tiny skill with a known-dead link, a known-deprecated model ID and a stale `last-verified` date. Every nightly run must report exactly those three findings, deduplicated against its standing issue. If a run reports fewer, the checks are broken, and the bot raises a `freshness-alert` instead of a green result. Its evals have one positive prompt that must fire, which proves the eval harness and the CLIs work end to end. `zz-canary` is excluded from `catalog.json`, so consumers never see it.

### 7.7 Cost and budget

- Static checks and the watch cost nothing in tokens. The one LLM classification call per changed upstream source is capped at 10 per day.
- Evals run only for skills whose content changed, or for every skill when a target CLI version changes (a router change is the main regression source). Otherwise each skill re-runs on a weekly rotation.
- Per-run ceiling, per-month ceiling and runs per case follow open question 6 (recommended: $5, $40, 3). The runner tracks spend from the CLIs' JSON usage output. When the next case would cross the ceiling, it stops, records the skipped skills, and raises a `freshness-alert` "budget reached", so a budget stop is never silent.
- Models are pinned per eval run (the agent-skills notes show results change with the default model), and the pin itself is checked against the model ID table in 7.1.

## 8. Sync and bump flow

1. A change merges to the catalog's `main`, CI regenerates `catalog.json`, and a release tag `v<yyyy.mm.dd>.<n>` is cut when any skill version changed.
2. The bump bot (M5), for each consumer in `consumers.json`:
   - updates the gitlink to the tag's commit, and runs `tools/link-consumer` to add or remove the `.claude/skills/<name>` links;
   - opens one PR titled `skills: bump shared to <tag>`. The body lists each changed skill with its old and new version, the changelog lines, any skill marked `stale`, and the freshness status of that commit;
   - does not open a new PR if one is already open; it updates it instead.
3. Consumer CI runs `link-consumer --check` and its own tests. Review follows the consumer's normal rules (cross-review, then Kenny, since protected paths change).
4. Rollback is reverting the bump PR.

The diff of a bump PR is the gitlink line plus link additions or removals. "One-line" holds whenever the set of shared skills does not change.

## 9. Third-party skills

- Third-party skills stay in their own clones and plugin installs. The catalog holds none by default.
- An optional M7 index (`index/third-party.json`) records name, description, source repo, path, license and sha for skills in a list of watched public repositories, served read-only through the MCP server's `search_skills(scope="third-party")`. It holds no bodies.
- **Promotion** copies a third-party skill into `skills/<name>/`, with `catalog-provenance: promoted`, the source URL, a 40-hex `catalog-source-sha`, and a `catalog-source-license` that must be on the allowlist (MIT, Apache-2.0, BSD-2/3-Clause, ISC, CC-BY-4.0). The upstream `LICENSE` is kept beside it. The promotion is a PR that Kenny approves. After that, 7.4 watches it.

## 10. Versioning

- Each skill carries a semver `catalog-version`. Major: the skill's contract changes (inputs, the order of steps a consumer relies on, a renamed skill). Minor: new guidance. Patch: wording and fixes. The bot's mechanical PRs are patches.
- Each skill has a `## Changelog` table at the end, following the research-mcp convention.
- The catalog is tagged by date (`v2026.11.11.1`). Consumers pin commits, and bump PRs reference tags.
- A rename keeps the old name for one release as a stub skill whose description says "renamed to X". The linter then fails the following release until the stub is removed.

## 11. Rejected alternatives

| Alternative | Why rejected |
| --- | --- |
| **Symlinks from bot repos to a catalog checkout** (`.agents/skills/shared -> ~/Documents/GitHub/skills-catalog/skills`) | iwb and the orchestrator work in fresh clones and worktrees, where an absolute link to Kenny's home folder dangles or, worse, resolves to whatever that working tree holds. A link is not pinned, so skills can change mid-run. The orchestrator's lessons step writes into `.agents/skills` and would write through the link into the catalog working tree. Codex's sandbox may refuse reads outside the repository. (In-repo relative links into the pinned submodule, section 4.3, have none of these problems.) |
| **A live MCP server as the bots' skill source** | Claude Code and Codex load skills from disk, not from MCP, so bots would need a sync step at run start anyway. A server is one more thing that can be down, and a down server means silently missing skills. Skills would change mid-run and could not be reproduced afterwards. The Skills extension is experimental, and few hosts speak it. |
| **Serving the whole `~/Documents/GitHub` skill folder (all 1,771 files)** | About 84% is third-party content with mixed licenses and no review. It would publish other people's work and private-repo content, flood agents' skill lists (Codex drops skills over its 2% context budget, and Claude Code truncates descriptions), and keep every duplicate this project exists to remove. |
| A git subtree or vendored copy per consumer | Copies drift again; the update is a large diff instead of one line; the catalog's history is lost in each consumer. |
| A package (npm or PyPI) per release | Adds a registry and an install step to every bot clone for what is a folder of Markdown; it does not pin any better than a gitlink. |
| Claude Code plugin marketplace as the only channel | Codex does not read Claude plugins, and plugin skills are namespaced (`/plugin:skill`), which changes how they trigger. It may be added later as an extra channel for interactive use. |
