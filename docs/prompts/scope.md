# Scope Phase Prompt

> Workflow phase: SCOPING
> CLI: `claude -p` and `codex exec --json`
> Output artifacts: `scoping/claude-scope-*.md`, `scoping/codex-scope-*.md`, canonical `scoping/scope-<run>.md`
> Contract: canonical `scope.md` with YAML frontmatter

## Purpose

Validate and normalize the raw task before planning begins. Scoping is a six-round Claude/Codex debate that relies on session continuity instead of re-injecting all previous context into every prompt.

The canonical scope must include YAML frontmatter fields:
`normalized_task`, `complexity_tier`, `actionable`, `key_files`, and `context`.

## Debate Flow

1. Claude R1: `build_prescope_claude_prompt(raw_task)` drafts canonical `scope.md`.
2. Codex R2: `build_prescope_codex_prompt(raw_task, repo_summary, directory_tree)` drafts independent `codex-scope.md`.
3. Codex R3: `build_scope_compare_codex_prompt(claude_scope_md)` reviews Claude's canonical scope in the resumed Codex thread.
4. Claude R4: `build_scope_respond_claude_prompt(scope_md, codex_scope_md)` accepts or pushes back in the resumed Claude session.
5. Codex R5: `build_scope_final_codex_prompt(claude_scope_md)` makes the final Codex case in the resumed Codex thread.
6. Claude R6: `build_scope_final_claude_prompt(scope_md, codex_scope_md)` produces the final canonical scope.

## Templates

Prescope prompts ask the agent to scope the task without modifying code. Claude returns canonical markdown with required YAML frontmatter. Codex returns independent markdown notes with normalized task, actionability, complexity, key files or areas, assumptions, and risks.

Codex comparison and final prompts include Claude's current canonical scope and ask for `codex-scope.md` markdown beginning with:

```yaml
agreement: true|false
```

Claude response and final prompts include Codex's latest reasoning and ask for canonical `scope.md` markdown.
