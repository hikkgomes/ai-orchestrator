"""Git worktree lifecycle manager.

Creates and removes a single ephemeral worktree per run.  All mutating steps
execute sequentially in this worktree so that each step sees prior step output
(DD-1).

Worktree naming: ``aio/run-<short-uuid>``
Worktree path:   ``.ai-orchestrator/worktrees/run-<short-uuid>``

Merge pre-checks (DD-6):
1. Base branch working tree must be clean (no uncommitted changes).
2. Base commit SHA must match what was recorded at worktree creation.
   If diverged, user must explicitly force-approve.

See docs/workflow.md Phase 7 (MERGING) for the full merge sequence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(Exception):
    """Raised when a worktree operation fails."""


class WorktreeManager:
    """Manages the git worktree lifecycle for a single run.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root (where ``.git/`` lives).
    artifact_root:
        Path to the ``.ai-orchestrator/`` directory (worktrees live here).
    """

    def __init__(self, repo_root: Path, artifact_root: Path) -> None:
        self._repo_root = repo_root
        self._worktrees_dir = artifact_root / "worktrees"

    def create(self, run_id: str, base_branch: str, branch_prefix: str) -> tuple[Path, str, str]:
        """Create a new worktree for *run_id*.

        Creates branch ``<branch_prefix>run-<short_run_id>`` and checks it out
        at ``<artifact_root>/worktrees/run-<short_run_id>``.

        Parameters
        ----------
        run_id:
            Full UUID of the run.
        base_branch:
            Branch to fork from (e.g. ``"main"``).
        branch_prefix:
            Prefix for the new branch (e.g. ``"aio/"``).

        Returns
        -------
        tuple[Path, str, str]
            ``(worktree_path, branch_name, base_commit_sha)``

        Raises
        ------
        WorktreeError
            If ``git worktree add`` fails.
        """
        short_run_id = run_id[:8]
        branch_name = f"{branch_prefix}run-{short_run_id}"
        worktree_path = self._worktrees_dir / f"run-{short_run_id}"
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

        if worktree_path.exists():
            raise WorktreeError(f"Worktree path already exists: {worktree_path}")

        base_commit = _get_head_sha(self._repo_root, base_branch)
        _run_git(
            ["worktree", "add", "-b", branch_name, str(worktree_path), base_branch],
            cwd=self._repo_root,
        )
        return worktree_path, branch_name, base_commit

    def remove(self, worktree_path: Path, branch_name: str, *, force: bool = False) -> None:
        """Remove a worktree and delete its branch.

        Parameters
        ----------
        force:
            Pass ``--force`` to ``git worktree remove``.
        """
        if worktree_path.exists():
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(worktree_path))
            _run_git(args, cwd=self._repo_root)

        delete_flag = "-D" if force else "-d"
        completed = subprocess.run(
            ["git", "branch", delete_flag, branch_name],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0 and "not found" not in completed.stderr.lower():
            raise WorktreeError(
                f"Failed to delete branch {branch_name}: {completed.stderr.strip()}"
            )

    def reset(self, worktree_path: Path) -> None:
        """Reset a worktree back to its last committed state."""
        _run_git(["reset", "--hard", "HEAD"], cwd=worktree_path)
        _run_git(["clean", "-fd"], cwd=worktree_path)

    def verify_merge_preconditions(
        self,
        base_branch: str,
        recorded_base_commit: str,
        *,
        allow_base_commit_mismatch: bool = False,
    ) -> None:
        """Assert merge pre-conditions are met.

        Checks:
        1. Base branch working tree is clean.
        2. Current HEAD of base branch matches *recorded_base_commit*.

        Raises
        ------
        WorktreeError
            With a descriptive message if either check fails.
        """
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if status.returncode != 0:
            raise WorktreeError(f"Failed to inspect working tree: {status.stderr.strip()}")
        if status.stdout.strip():
            raise WorktreeError("Base branch working tree is dirty; commit or stash changes first.")

        current_base_commit = _get_head_sha(self._repo_root, base_branch)
        if current_base_commit != recorded_base_commit and not allow_base_commit_mismatch:
            raise WorktreeError("Base branch has advanced; merge requires explicit force approval.")

    def merge(
        self,
        base_branch: str,
        worktree_branch: str,
        task_summary: str,
    ) -> None:
        """Merge *worktree_branch* into *base_branch* with ``--no-ff``.

        Raises
        ------
        WorktreeError
            On conflict or other merge failure.
        """
        _run_git(["checkout", base_branch], cwd=self._repo_root)
        completed = subprocess.run(
            ["git", "merge", "--no-ff", worktree_branch, "-m", f"aio: {task_summary}"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode == 0:
            return

        output = f"{completed.stdout}\n{completed.stderr}".strip()
        if "CONFLICT" in output:
            raise WorktreeError("Merge conflict detected")
        raise WorktreeError(
            f"git merge failed (exit {completed.returncode}): {completed.stderr.strip() or completed.stdout.strip()}"
        )

    def has_unresolved_conflicts(self) -> bool:
        """Return True if the repository currently has unresolved merge conflicts."""
        completed = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            raise WorktreeError(f"Failed to inspect merge conflicts: {completed.stderr.strip()}")
        return bool(completed.stdout.strip())

    def continue_merge(self) -> None:
        """Continue a previously conflicted merge after the user resolves conflicts."""
        if self.has_unresolved_conflicts():
            raise WorktreeError("Merge conflicts are still unresolved.")
        completed = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            if "nothing to commit" in stderr.lower() or "nothing to commit" in stdout.lower():
                return
            raise WorktreeError(
                f"Failed to continue merge: {stderr or stdout}"
            )

    def list_worktrees(self) -> list[Path]:
        """Return known worktree directories under the artifact root."""
        if not self._worktrees_dir.exists():
            return []
        return sorted(path for path in self._worktrees_dir.iterdir() if path.is_dir())


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result.  Raises ``WorktreeError`` on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result


def _get_head_sha(repo_root: Path, revision: str) -> str:
    result = _run_git(["rev-parse", revision], cwd=repo_root)
    return result.stdout.strip()
