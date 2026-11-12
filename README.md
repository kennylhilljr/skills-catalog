# skills-catalog

One source of truth for Kenny's own agent skills (`SKILL.md` folders for Claude Code and Codex). The goal is one owner and one copy per skill, read by bots at a pinned commit and kept current by a freshness bot.

**Status: design, M1 starting.** This repository holds the inventory, the design and the spec. The owner's decisions on the design's questions are recorded in DESIGN section 0. One question is still open there.

The canonical set is 22 skills: 12 delivered to consumers and 10 `sdd-*` skills that are **held**. Held skills are published under `.held/` and checked like any other skill, but no consumer reads them until the SDD switch gate passes (DESIGN 3.2).

| Document | What it is |
| --- | --- |
| [docs/DESIGN.md](docs/DESIGN.md) | Decisions and the open question, architecture, held skills, the freshness bot and its alerts, the security model and rejected alternatives |
| [docs/SPEC.md](docs/SPEC.md) | Requirements and EARS acceptance criteria for milestones M1 to M7 |
| [inventory/own-skills.md](inventory/own-skills.md) | Every own skill found on the machine, duplicate clusters, and the proposed canonical set |
| [inventory/own-skills.json](inventory/own-skills.json) | The same, machine-readable |
| [AGENTS.md](AGENTS.md) | How agents work in this repository |

## How it will work

- **Catalog**: `skills/<name>/SKILL.md`. Frontmatter uses only the six Agent Skills spec fields, and catalog fields (owner, version, provenance, last verified, targets) go under `metadata`.
- **Bots** consume the catalog as a git submodule at `.agents/skills/shared`, pinned to a commit and updated by reviewed bump PRs. In-repo relative links under `.claude/skills/` make the skills visible to Claude Code.
- **Interactive agents** can read skills through a FastMCP server (`skill://` resources and the experimental Skills extension).
- **Freshness bot**: it watches Claude Code, Codex, the Agent Skills spec, FastMCP and the model lists for changes, and checks every skill nightly and on every PR. It runs behavioral evals on the Mac only when something changed, through the bots' own CLIs, and turns findings into PRs and issues. Alerts are issues that iwb can pick up, plus a Slack message.
- **Third-party skills stay out.** They can be promoted only after review, with provenance, a license and a pinned source sha.

## Regenerating the inventory

```sh
python3 tools/inventory.py            # scans ~/Documents/GitHub and the global skill folders
python3 tools/inventory.py --help
```

The script needs Python 3.9+ and `gh` (for repository visibility). Skills named in the owner's untracked local exclusion list, `~/.config/skills-catalog/exclude.txt`, are skipped entirely (DESIGN 5.1). `--offline --visibility file.json` skips `gh`. Judgment calls go in `inventory/decisions.json` and `inventory/provenance-overrides.json`, not in the generated files.

## License

MIT for the catalog's own content, once a `LICENSE` is added in M1. Promoted third-party skills keep their own license beside them.
