---
name: agent-debug
description: Debug a misbehaving LLM agent with an ordered diagnostic checklist covering tool schemas, context pressure, tool call pairing, loop termination, prompt conflicts, error handling, model tier and rate limits. Use when an agent loops, stalls, fails tool calls, truncates or returns unexpected output.
metadata:
  catalog-version: "1.0.0"
  catalog-owner: "kennylhilljr"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
---

When an agent misbehaves (wrong output, an endless loop, tool errors, truncated context or unexpected termination), work through this checklist in order. Stop at the first finding and fix it before continuing.

## Diagnostic checklist

**1. Tool schema validity**
- Print all tool definitions. Check: every required field has a type, `description` is present and accurate, no conflicting parameter names.
- Common failure: `required` array lists a field not in `properties`.

**2. Context window pressure**
- Count tokens in the current conversation. If above 150k, the model may truncate or hallucinate tool results.
- Fix: inject a context summary block and trim old tool results.

**3. Tool call / result pairing**
- Verify every `tool_use` block has a matching `tool_result` block in the correct turn order.
- A missing `tool_result` causes the API to return a 400 on the next turn.

**4. Loop termination conditions**
- Is there a clear stop condition? Check: max_iterations guard, explicit DONE signal from the model, empty tool_calls response.
- Common failure: agent loops forever because the stop condition depends on a string match that never exactly occurs.

**5. System prompt / tool conflict**
- Does the system prompt tell the agent to do something a tool's description says not to do? That creates unpredictable behavior.
- Fix: align the system prompt and tool descriptions.

**6. Error state leakage**
- When a tool returns an error, does the agent handle it or loop trying the same call?
- Add explicit error handling: if `tool_result` starts with "Error:", the agent should try an alternative or stop.

**7. Model capability mismatch**
- If using haiku for complex multi-step reasoning, switch to sonnet or opus.
- Check the task complexity vs. model tier.

**8. Rate limits / timeouts**
- Add exponential backoff. Check logs for 529 (overloaded) or 408 (timeout) responses.

## Output format

After running through the checklist, report:
- Which check failed first
- Exact evidence (log line, message structure, token count)
- One concrete fix to apply
- How to verify the fix worked

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.0.0 | 2026-11-11 | Imported from `~/.claude/skills/agent-debug` (local only, no repository; sha256 `f7e6a63b4540`); description given a use clause, dashes replaced |
