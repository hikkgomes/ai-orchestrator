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
    collect_file_context,
    redact_secret_text,
    render_directory_tree,
)


def test_collect_file_context_excludes_secret_like_content(tmp_path):
    (tmp_path / "safe.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=abc123\n", encoding="utf-8")

    context, skipped = collect_file_context(tmp_path, ["safe.txt", ".env"])

    assert "safe.txt" in context
    assert ".env" in skipped
    assert "TOKEN=abc123" not in context


def test_render_directory_tree_truncates_depth(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("x\n", encoding="utf-8")

    tree = render_directory_tree(tmp_path, max_depth=2)

    assert tmp_path.name in tree
    assert "file.txt" not in tree


def test_collect_file_context_does_not_treat_environment_py_as_env_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "environment.py").write_text("CONFIG = 'safe'\n", encoding="utf-8")

    context, skipped = collect_file_context(tmp_path, ["src/environment.py"])

    assert "environment.py" in context
    assert skipped == []


def test_redact_secret_text_hides_env_diff_and_inline_tokens():
    diff = "\n".join(
        [
            "diff --git a/.env.local b/.env.local",
            "--- a/.env.local",
            "+++ b/.env.local",
            "+API_KEY=supersecretvalue12345",
            "diff --git a/app.py b/app.py",
            '+token = "supersecretvalue12345"',
        ]
    )

    redacted = redact_secret_text(diff)

    assert "supersecretvalue12345" not in redacted
    assert "[REDACTED SECRET-BEARING DIFF CONTENT]" in redacted
    assert "[REDACTED SECRET]" in redacted


def test_retry_prompt_includes_original_context():
    original_prompt = "STEP:\nImplement feature\n\nOUTPUT SCHEMA:\n{}"

    prompt = build_retry_prompt(
        original_prompt=original_prompt,
        error_message="missing required field",
    )

    assert "missing required field" in prompt
    assert "The full original prompt follows." in prompt
    assert original_prompt in prompt


def test_retry_prompt_relay_omits_original_context():
    original_prompt = "STEP:\nImplement feature\n\nOUTPUT SCHEMA:\n{}"

    prompt = build_retry_prompt(
        original_prompt=original_prompt,
        error_message="missing required field",
        relay=True,
    )

    assert "missing required field" in prompt
    assert "The full original prompt follows." not in prompt
    assert original_prompt not in prompt


def test_build_full_execution_prompt_renders_single_result_path():
    prompt = build_full_execution_prompt(
        plan_text="## Steps\n- Update endpoint",
        result_file_path="/tmp/execution.json",
        schema_json='{"title":"ExecutionResult"}',
    )

    assert "FULLY IMPLEMENT THE PLAN ABOVE" in prompt
    assert "write your result JSON to:" in prompt
    assert "/tmp/execution.json" in prompt
    assert "src/api.py" not in prompt


def test_build_full_execution_prompt_relay_omits_schema_json():
    prompt = build_full_execution_prompt(
        plan_text="## Steps\n- Update endpoint",
        result_file_path="/tmp/execution.json",
        schema_json='{"title":"ExecutionResult"}',
        relay=True,
    )

    assert "/tmp/execution.json" in prompt
    assert '{"title":"ExecutionResult"}' not in prompt
    assert "Required JSON fields:" in prompt


def test_build_review_prompt_renders_optional_reviewer_sections():
    prompt = build_review_prompt(
        git_diff="diff --git a/a.py b/a.py",
        step_results_json='[{"step_number":1}]',
        schema_json='{"title":"Review"}',
        heuristic_findings=[
            {
                "workspace": "",
                "rule_id": "placeholder",
                "file": "src/app.py",
                "line": 4,
                "snippet": 'dummy_key = "changeme"',
            }
        ],
        review_categories={
            "hallucinated_api": "Non-existent APIs, methods, flags, or parameters.",
            "runtime_breakage": "Code that appears plausible but will not run.",
        },
        reviewer_config={
            "project": {"stack": ["python", "fastapi"]},
            "paths": {"critical": ["src/auth/"]},
            "risk": {"auth_sensitive": ["middleware.py"]},
            "architecture": {"patterns": ["layered"], "key_libraries": {}, "naming": {}, "project_description": ""},
        },
    )

    assert "HEURISTIC SCAN RESULTS:" in prompt
    assert '[placeholder] src/app.py:4 :: dummy_key = "changeme"' in prompt
    assert "AI FAILURE CATEGORIES:" in prompt
    assert "hallucinated_api - Non-existent APIs" in prompt
    assert "REPOSITORY CONTEXT:" in prompt
    assert "Stack: python, fastapi" in prompt


def test_build_prescope_codex_prompt_relay_omits_repo_tree():
    prompt = build_prescope_codex_prompt(
        "Implement health checks",
        "Repository summary block",
        "TREE DATA BLOCK",
        relay=True,
    )
    assert "Repository summary block" not in prompt
    assert "TREE DATA BLOCK" not in prompt


def test_scope_debate_prompts_relay_are_lean():
    other_output = "---\nagreement: false\n---\nneeds changes"
    for builder in (
        build_scope_compare_codex_prompt,
        build_scope_respond_claude_prompt,
        build_scope_final_codex_prompt,
        build_scope_final_claude_prompt,
    ):
        prompt = builder(other_output, relay=True)
        assert "I had another analysis of this task:" in prompt
        assert other_output in prompt
        assert "agreement: true" in prompt


def test_build_review_prompt_relay_omits_heavy_sections_and_keeps_heuristics():
    prompt = build_review_prompt(
        git_diff="diff --git a/a.py b/a.py\n+print('x')",
        step_results_json='[{"step_number":1}]',
        schema_json='{"title":"Review"}',
        heuristic_findings=[
            {
                "workspace": "",
                "rule_id": "placeholder",
                "file": "src/app.py",
                "line": 4,
                "snippet": 'dummy_key = "changeme"',
            }
        ],
        review_categories={"runtime_breakage": "desc"},
        reviewer_config={"project": {"stack": ["python"]}},
        relay=True,
    )

    assert "HEURISTIC SCAN RESULTS:" in prompt
    assert "placeholder" in prompt
    assert "IMPLEMENTATION DIFF:" not in prompt
    assert "AI FAILURE CATEGORIES:" not in prompt
    assert "REPOSITORY CONTEXT:" not in prompt
    assert '{"title":"Review"}' not in prompt


def test_build_review_codex_prompt_relay_omits_diff_and_schema():
    prompt = build_review_codex_prompt(
        task_description="Task body",
        git_diff="diff --git ...",
        review_json='{"summary":"ok"}',
        schema_json='{"title":"Review"}',
        relay=True,
    )
    assert '{"summary":"ok"}' in prompt
    assert "IMPLEMENTATION DIFF:" not in prompt
    assert "OUTPUT SCHEMA:" not in prompt


def test_build_review_final_claude_prompt_relay_omits_schema():
    prompt = build_review_final_claude_prompt(
        codex_review_json='{"summary":"pushback"}',
        schema_json='{"title":"Debate"}',
        relay=True,
    )
    assert '{"summary":"pushback"}' in prompt
    assert "OUTPUT SCHEMA:" not in prompt
