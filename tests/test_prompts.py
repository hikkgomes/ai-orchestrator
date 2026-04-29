from __future__ import annotations

from ai_orchestrator.prompts.templates import (
    build_prescope_codex_prompt,
    build_scope_compare_codex_prompt,
    build_scope_respond_claude_prompt,
    build_scope_final_codex_prompt,
    build_scope_final_claude_prompt,
    build_full_execution_prompt,
    build_review_prompt,
    build_review_codex_prompt,
    build_review_final_claude_prompt,
    build_retry_prompt,
)


def test_retry_prompt_includes_original_context():
    original_prompt = "STEP:\nImplement feature\n\nOUTPUT SCHEMA:\n{}"

    prompt = build_retry_prompt(
        original_prompt=original_prompt,
        error_message="missing required field",
    )

    assert "missing required field" in prompt
    assert "The full original prompt follows." in prompt
    assert original_prompt in prompt


def test_build_full_execution_prompt_renders_single_result_path():
    prompt = build_full_execution_prompt(
        plan_text="## Steps\n- Update endpoint",
        result_file_path="/tmp/execution.json",
    )

    assert "FULLY IMPLEMENT THE PLAN ABOVE" in prompt
    assert "write your result JSON to:" in prompt
    assert "/tmp/execution.json" in prompt
    assert "src/api.py" not in prompt


def test_build_full_execution_prompt_omits_schema_json():
    prompt = build_full_execution_prompt(
        plan_text="## Steps\n- Update endpoint",
        result_file_path="/tmp/execution.json",
    )

    assert "/tmp/execution.json" in prompt
    assert '{"title":"ExecutionResult"}' not in prompt
    assert "Required JSON fields (no extra fields allowed):" in prompt
    assert "no extra fields allowed" in prompt


def test_build_review_prompt_renders_optional_reviewer_sections():
    prompt = build_review_prompt(
        step_results_json='[{"step_number":1}]',
        heuristic_findings=[
            {
                "workspace": "",
                "rule_id": "placeholder",
                "file": "src/app.py",
                "line": 4,
                "snippet": 'dummy_key = "changeme"',
            }
        ],
    )

    assert "HEURISTIC SCAN RESULTS:" in prompt
    assert '[placeholder] src/app.py:4 :: dummy_key = "changeme"' in prompt
    assert "AI FAILURE CATEGORIES:" not in prompt
    assert "REPOSITORY CONTEXT:" not in prompt


def test_build_prescope_codex_prompt_omits_repo_tree():
    prompt = build_prescope_codex_prompt("Implement health checks")
    assert "Repository summary block" not in prompt
    assert "TREE DATA BLOCK" not in prompt


def test_scope_compare_codex_prompt_is_lean():
    other_output = "---\nagreement: false\n---\nneeds changes"
    prompt = build_scope_compare_codex_prompt(other_output)
    assert "I had another analysis of this task:" in prompt
    assert other_output in prompt
    assert "agreement: true" in prompt


def test_scope_respond_claude_prompt_requires_frontmatter():
    other_output = "---\nagreement: false\n---\nneeds changes"
    prompt = build_scope_respond_claude_prompt(other_output)
    assert "Codex reviewed your scope and has feedback:" in prompt
    assert "agreement: true" in prompt
    assert "Preserve YAML frontmatter" in prompt


def test_scope_final_codex_prompt_is_final_case():
    other_output = "---\nagreement: false\n---\nneeds changes"
    prompt = build_scope_final_codex_prompt(other_output)
    assert "Claude still disagrees. Make your final case:" in prompt
    assert "agreement: true" in prompt


def test_scope_final_claude_prompt_requires_canonical_scope():
    other_output = "---\nagreement: false\n---\nneeds changes"
    prompt = build_scope_final_claude_prompt(other_output)
    assert "You have the final say on the scope." in prompt
    assert "Preserve YAML frontmatter" in prompt


def test_build_review_prompt_omits_heavy_sections_and_keeps_heuristics():
    prompt = build_review_prompt(
        step_results_json='[{"step_number":1}]',
        heuristic_findings=[
            {
                "workspace": "",
                "rule_id": "placeholder",
                "file": "src/app.py",
                "line": 4,
                "snippet": 'dummy_key = "changeme"',
            }
        ],
    )

    assert "HEURISTIC SCAN RESULTS:" in prompt
    assert "placeholder" in prompt
    assert "IMPLEMENTATION DIFF:" not in prompt
    assert "AI FAILURE CATEGORIES:" not in prompt
    assert "REPOSITORY CONTEXT:" not in prompt
    assert '{"title":"Review"}' not in prompt
    assert "If you cannot inspect the worktree, return verdict=request_changes" in prompt
    assert "Use verdict=approve and blocks_merge=false only if the implementation should proceed." in prompt
    assert "reject requires blocks_merge=true and at least one critical or major finding." in prompt
    assert "No extra fields allowed." in prompt


def test_build_review_prompt_empty_heuristics():
    prompt = build_review_prompt(
        step_results_json='[{"step_number":1}]',
        heuristic_findings=None,
    )
    assert "HEURISTIC SCAN RESULTS:" not in prompt
    assert "EXECUTION RESULTS:" in prompt


def test_build_review_codex_prompt_omits_diff_and_schema():
    prompt = build_review_codex_prompt(
        task_description="Task body",
        review_json='{"summary":"ok"}',
    )
    assert "Task: Task body" in prompt
    assert '{"summary":"ok"}' in prompt
    assert "If you cannot inspect the worktree, return verdict=request_changes" in prompt
    assert "IMPLEMENTATION DIFF:" not in prompt
    assert "OUTPUT SCHEMA:" not in prompt
    assert "Use verdict=approve and blocks_merge=false only if the implementation should proceed." in prompt
    assert "reject requires blocks_merge=true and at least one critical or major finding." in prompt


def test_build_review_final_claude_prompt_omits_schema():
    prompt = build_review_final_claude_prompt(
        codex_review_json='{"summary":"pushback"}',
    )
    assert '{"summary":"pushback"}' in prompt
    assert "OUTPUT SCHEMA:" not in prompt
