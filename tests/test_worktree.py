from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_orchestrator.bootstrap import ensure_runtime_gitignore
from ai_orchestrator.worktree import WorktreeError, WorktreeManager


def test_worktree_create_merge_cleanup_cycle(tmp_repo, artifact_root):
    manager = WorktreeManager(tmp_repo, artifact_root)
    worktree_path, branch_name, base_commit = manager.create(
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "main",
        "aio/",
    )

    assert worktree_path.exists()
    assert branch_name == "aio/run-aaaaaaaa"
    assert base_commit

    feature_file = worktree_path / "feature.txt"
    feature_file.write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=worktree_path, check=True, capture_output=True)

    manager.verify_merge_preconditions("main", base_commit)
    manager.merge("main", branch_name, "feature work")
    assert (tmp_repo / "feature.txt").exists()

    manager.remove(worktree_path, branch_name, force=True)
    remaining = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=tmp_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert remaining.stdout.strip() == ""


def test_worktree_merge_conflict_detection(tmp_repo, artifact_root):
    manager = WorktreeManager(tmp_repo, artifact_root)
    (tmp_repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_repo, check=True, capture_output=True)

    worktree_path, branch_name, base_commit = manager.create(
        "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee",
        "main",
        "aio/",
    )
    (worktree_path / "README.md").write_text("worktree\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "worktree"], cwd=worktree_path, check=True, capture_output=True)

    (tmp_repo / "README.md").write_text("main\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "main"], cwd=tmp_repo, check=True, capture_output=True)

    with pytest.raises(WorktreeError, match="conflict"):
        manager.merge("main", branch_name, "conflict")

    assert manager.has_unresolved_conflicts() is True
    subprocess.run(["git", "merge", "--abort"], cwd=tmp_repo, check=True, capture_output=True)
    manager.remove(worktree_path, branch_name, force=True)


def test_worktree_manager_reset(tmp_repo, artifact_root):
    manager = WorktreeManager(tmp_repo, artifact_root)
    worktree_path, branch_name, _ = manager.create(
        "99999999-bbbb-cccc-dddd-eeeeeeeeeeee",
        "main",
        "aio/",
    )

    (worktree_path / "README.md").write_text("modified\n", encoding="utf-8")
    (worktree_path / "scratch.txt").write_text("temp\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md", "scratch.txt"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
    )

    manager.reset(worktree_path)

    assert (worktree_path / "README.md").read_text(encoding="utf-8") == "# Test repo\n"
    assert not (worktree_path / "scratch.txt").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""
    cached_diff = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert cached_diff.stdout.strip() == ""

    manager.remove(worktree_path, branch_name, force=True)


def test_merge_preconditions_allow_generated_gitignore(tmp_repo, artifact_root):
    manager = WorktreeManager(tmp_repo, artifact_root)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    ensure_runtime_gitignore(tmp_repo)

    manager.verify_merge_preconditions("main", base_commit)


def test_merge_preconditions_reject_user_gitignore_changes(tmp_repo, artifact_root):
    manager = WorktreeManager(tmp_repo, artifact_root)
    (tmp_repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add gitignore"], cwd=tmp_repo, check=True, capture_output=True)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_repo / ".gitignore").write_text("dist/\n.env\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="dirty"):
        manager.verify_merge_preconditions("main", base_commit)
