"""Pytest shared fixtures for ai-orchestrator tests.

All tests use mock adapters unless explicitly marked as integration tests.
Integration tests that invoke real CLIs are opt-in (marked with
``@pytest.mark.integration``) and skipped by default.

Common fixtures:
- ``tmp_repo``: a real git repo in a temp directory (for worktree tests)
- ``artifact_root``: a temporary ``.ai-orchestrator/`` directory
- ``default_config``: a Config object with default values
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ai_orchestrator.config import Config


@pytest.fixture()
def artifact_root(tmp_path: Path) -> Path:
    """Return a temporary .ai-orchestrator/ directory."""
    root = tmp_path / ".ai-orchestrator"
    for subdir in (
        "state",
        "plans",
        "results",
        "reviews",
        "adjudications",
        "logs",
        "worktrees",
        "prompts",
        "approvals",
        "feedback",
        "executions",
    ):
        (root / subdir).mkdir(parents=True)
    return root


@pytest.fixture()
def default_config() -> Config:
    """Return a Config object with all defaults."""
    return Config()


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository in a temp directory.

    Returns the path to the repo root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True, capture_output=True)
    # Create an initial commit so the repo has a HEAD
    (repo / "README.md").write_text("# Test repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    return repo
