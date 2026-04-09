from __future__ import annotations

from ai_orchestrator.prompts.templates import (
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
