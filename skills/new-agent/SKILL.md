---
name: new-agent
description: Scaffold a new Claude agent project with AGENTS.md, CLAUDE.md, a system prompt, tool definitions, a Python entry point and an evaluation harness. Use when asked to create, start or scaffold a new agent project.
metadata:
  catalog-version: "1.0.0"
  catalog-owner: "kennylhilljr"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
  catalog-requires: "python3"
---

Scaffold a new Claude agent project. Ask the user for:
1. Agent name (kebab-case)
2. Agent purpose in one sentence
3. Primary tools it needs (web search, file ops, code exec, external APIs, etc.)
4. Autonomy level: single-turn, multi-turn loop, or fully autonomous

Create `<name>/` in the folder where the user keeps their repositories (ask if unsure; for example `~/Documents/GitHub/<name>/`) with:

**Core files:**
- `AGENTS.md`: agent contract: purpose, capabilities, tool permissions, loop termination conditions, failure modes
- `CLAUDE.md`: Claude Code context: how to develop and test this agent
- `system_prompt.md`: the agent's system prompt, structured as: role, tools available, rules, output format
- `README.md`: what the agent does, how to run it, configuration

**Entry point (Python preferred):**
- `agent.py` with:
  - `anthropic` client initialized from `ANTHROPIC_API_KEY` env var
  - Tool definitions as typed dicts or Pydantic models
  - Main loop with tool use handling
  - Clean error handling and exit conditions
  - Logging to stderr (never stdout if outputting structured data)

**Evaluation harness:**
- `evals/` directory
- `evals/test_cases.json`: 3-5 example inputs with expected outputs/behaviors
- `evals/run_evals.py`: runs test cases and reports pass/fail

**Configuration:**
- `.env.example` listing required env vars (ANTHROPIC_API_KEY plus any others)
- `pyproject.toml` or `requirements.txt`

After scaffolding, remind the user to:
1. Copy `.env.example` to `.env` and fill in values
2. Run `python evals/run_evals.py` to verify baseline behavior
3. Wire any needed MCPs into the project `.mcp.json`

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-11-11 | Imported from `~/.claude/skills/new-agent` (local only, no repository; sha256 `ecbbdf4512ef`); description given a use clause, target folder made a question, dashes replaced |
