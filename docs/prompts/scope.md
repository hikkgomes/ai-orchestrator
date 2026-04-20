# Scope Phase Prompt

> Workflow phase: SCOPING
> CLI: `claude -p` and `codex exec`
> Output artifacts: `scoping/claude-scope-*.md`, `scoping/codex-scope-*.md`, canonical `scoping/scope-<run>.md`
> Contract: canonical `scope.md` with YAML frontmatter

## Purpose

Validate and normalize the raw task string submitted via `orch new <task>`
before planning begins. Scoping is a bounded Claude/Codex debate:

1. Round 0 invokes Claude and Codex in parallel for independent pre-scope notes.
2. Claude synthesizes canonical `scope.md`.
3. Codex reviews the canonical scope.
4. If needed, Claude updates `scope.md`, Codex gives a final escalated
   assessment, and Claude makes the final scoping call.

Claude scoping rounds can use agentic tools (`Read`, `Grep`, `Glob`) to inspect
the repository directly. The provided directory tree is a lightweight starting
map, not the full context source.

The canonical scope must include YAML frontmatter fields:
`normalized_task`, `complexity_tier`, `actionable`, `key_files`, and `context`.

## Round 0 Template

```
You are {actor}, independently scoping a user request for an automated
software orchestrator.

Do not implement. Identify what the task really asks for, whether it is
actionable, likely key files or areas, risks, and a recommended complexity tier.
Return ONLY markdown notes.

RAW TASK:
{raw_task}

REPOSITORY SUMMARY:
{repo_summary}

REPOSITORY STRUCTURE (orientation only; use Read/Grep/Glob for details):
{directory_tree}
```

## Canonical Scope Template

```
---
normalized_task: "..."
complexity_tier: simple|moderate|complex|architectural|extramax
actionable: true|false
key_files:
  - path/or/area
context: "Assumptions, constraints, or blocking context."
---

Markdown explanation of the chosen scope.
```

## Review/Rebuttal Contract

Codex review artifacts are markdown files with YAML frontmatter containing
`agreement: true|false`. When `agreement` is false, the body must explain the
specific scope concerns. Codex never edits canonical `scope.md`.

Claude rebuttal artifacts update canonical `scope.md` directly, either
accepting Codex feedback or documenting why the final scope proceeds as written.
