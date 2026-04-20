---
name: skills
description: Index of all Claude Code skills used by ai-orchestrator workflow agents (planner, reviewer, replanner). Not directly invokable — see subdirectory skills.
---

# Claude Code Skills for ai-orchestrator

This directory contains Claude Code skill definitions used when Claude Code acts
as a workflow agent (planner, reviewer, replanner) within an
ai-orchestrator run.

Phase invocation examples:
```text
Planning/fix planning:
claude -p "<rendered prompt>" --allowedTools Read,Grep,Glob --output-format json

Reviewing:
claude -p "<rendered prompt>" --allowedTools Read,Grep,Glob,Bash --output-format json
```

Each invocation is still a fresh subprocess. Claude phases may share a unified
session via `--resume` when enabled. Codex phases are always fresh subprocesses.
See `AGENTS.md` for the full adapter contract.

---

## Skill: orchestration-architect

**Directory:** `.claude/skills/orchestration-architect/SKILL.md`
**Active phase:** PLANNING (initial plan)
**Prompt template:** `docs/prompts/plan.md`
**Expected output:** Markdown plan with `## Approach`, `## Steps`, `## Key Files`

Produces an implementation plan after exploring the codebase with Read/Grep/Glob.

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
**Expected output:** Incremental markdown plan with `## Approach`, `## Steps`, `## Key Files`

Produces an incremental fix plan on top of existing worktree changes.

---

## Notes

- Execution and review skills receive JSON schemas in the prompt.
- Planning skills produce markdown plans and are consumed through `invoke_text`.
- `--output-format json` is always passed. Planning extracts the text body from
  the JSON envelope (`invoke_text`); other phases parse the structured JSON
  content (`invoke`).
- For JSON-output phases, if the CLI returns non-JSON or schema-invalid JSON, the
  orchestrator retries up to `max_retries` times with explicit retry guidance.
- Reasoning effort is configurable per phase in `aio.toml` under `[routing.claude]`.
  Defaults: planner=high, reviewer=high.
