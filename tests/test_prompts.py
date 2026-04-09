from __future__ import annotations

from ai_orchestrator.prompts.templates import (
    build_feasibility_prompt_claude,
    build_feasibility_prompt_codex,
    build_review_prompt,
    build_scoping_prompt,
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


def test_build_scoping_prompt_renders_complexity_rules():
    prompt = build_scoping_prompt(
        raw_task="Fix typo in README",
        repo_summary="CLI orchestrator",
        directory_tree="repo\n  README.md",
        schema_json='{"title":"TaskDefinition"}',
    )

    assert "RAW TASK:\nFix typo in README" in prompt
    assert '"simple": single-file or config change' in prompt
    assert "OUTPUT SCHEMA:" in prompt


def test_build_feasibility_prompt_codex_renders_result_path():
    prompt = build_feasibility_prompt_codex(
        task_description="Add endpoint",
        plan_json='{"plan_id":"1"}',
        directory_tree="repo\n  src",
        result_file_path="/tmp/feasibility.json",
        schema_json='{"title":"FeasibilityResult"}',
    )

    assert "After checking, write your result JSON to:" in prompt
    assert "/tmp/feasibility.json" in prompt
    assert "Do NOT modify any source files." in prompt


def test_build_feasibility_prompt_claude_renders_static_analysis_rules():
    prompt = build_feasibility_prompt_claude(
        task_description="Add endpoint",
        plan_json='{"plan_id":"1"}',
        directory_tree="repo\n  src",
        schema_json='{"title":"FeasibilityResult"}',
    )

    assert "STATIC ANALYSIS" in prompt
    assert 'Identify any "files_to_modify" paths' in prompt
    assert "Respond with ONLY valid JSON." in prompt


def test_build_review_prompt_renders_optional_reviewer_sections():
    prompt = build_review_prompt(
        task_description="Implement feature",
        plan_json='{"plan_id":"1"}',
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
