from __future__ import annotations

from pathlib import Path

from ai_orchestrator.bootstrap import ensure_runtime_gitignore


def test_ensure_runtime_gitignore_migrates_legacy_block(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(
        "dist/\n\n"
        "# ai-orchestrator runtime artifacts\n"
        ".ai-orchestrator/state/\n"
        ".ai-orchestrator/results/\n"
        "\n"
        ".env\n",
        encoding="utf-8",
    )

    action = ensure_runtime_gitignore(tmp_path)

    assert action == "updated"
    assert gitignore.read_text(encoding="utf-8") == (
        "dist/\n\n"
        "# ai-orchestrator runtime artifacts\n"
        ".ai-orchestrator/\n"
        ".ai-review/\n"
        "\n"
        ".env\n"
    )


def test_ensure_runtime_gitignore_adds_missing_reviewer_ignore(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".ai-orchestrator/\n", encoding="utf-8")

    action = ensure_runtime_gitignore(tmp_path)

    assert action == "updated"
    assert ".ai-review/" in gitignore.read_text(encoding="utf-8")


def test_ensure_runtime_gitignore_keeps_existing_runtime_ignores(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".ai-orchestrator/\n.ai-review/\n", encoding="utf-8")

    action = ensure_runtime_gitignore(tmp_path)

    assert action is None
    assert gitignore.read_text(encoding="utf-8") == ".ai-orchestrator/\n.ai-review/\n"
