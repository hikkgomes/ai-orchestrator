---
name: skills
description: Index of all Claude Code skills used by ai-orchestrator workflow agents (planner, reviewer, replanner). Not directly invokable — see subdirectory skills.
---

# Claude Code Skills for ai-orchestrator

This directory contains Claude Code skill definitions used when Claude Code acts
as a workflow agent (planner, reviewer, replanner) within an
ai-orchestrator run.

Each skill is invoked via:
```
claude -p "<rendered prompt>" --output-format json
```

All invocations are fresh subprocesses. No transcript carry-over. The orchestrator
validates every response before acting on it. See `AGENTS.md` for the full
Claude adapter contract.

---

## Skill: orchestration-architect

**Directory:** `.claude/skills/orchestration-architect/SKILL.md`
**Active phase:** PLANNING (initial plan)
**Prompt template:** `docs/prompts/plan.md`
**Expected output:** Valid JSON matching `schemas/plan.schema.json`

Produces a decomposed implementation plan. The planner receives the task,
repository structure, and key file contents, and emits an ordered list of
implementation steps.

---

## Skill: orchestration-reviewer

**Directory:** `.claude/skills/orchestration-reviewer/SKILL.md`
**Active phase:** REVIEWING
**Prompt template:** `docs/prompts/review.md`
**Expected output:** Valid JSON matching `schemas/review.schema.json`

Reviews the full implementation diff independently of the planner and executor.
Produces findings with severity ratings and an overall verdict.

---

## Skill: fix-planner

**Directory:** `.claude/skills/fix-planner/SKILL.md`
**Active phase:** PLANNING (fix loop, triggered by review verdict = REPLAN)
**Prompt template:** `docs/prompts/fix-plan.md`
**Expected output:** Valid JSON matching `schemas/plan.schema.json`

Produces a corrected plan that addresses the structural failure identified in the
prior review. The new plan must differ meaningfully from the rejected plan.
Application-level validation enforces this.

---

## Notes

- Each skill receives the full JSON schema for its expected output embedded in the prompt.
- `--output-format json` is always passed.
- If the CLI returns non-JSON or schema-invalid JSON, the orchestrator retries up
  to `max_retries` times with an explicit error message in the retry prompt.
- Reasoning effort is configurable per phase in `aio.toml` under `[routing.claude]`.
  Defaults: planner=high, reviewer=high. See `docs/design-decisions.md`
  DD-17 for the graceful degradation strategy if the flag is not supported.
