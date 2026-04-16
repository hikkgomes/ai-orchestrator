"""Artifact storage helpers for workflow JSON files and prompt retention."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4


class ArtifactError(Exception):
    """Raised when an artifact cannot be read or written."""


class ArtifactStore:
    """Read and write orchestrator artifacts under ``.ai-orchestrator/``."""

    def __init__(self, artifact_root: Path, *, retain_prompts: bool = False) -> None:
        self._artifact_root = artifact_root
        self._retain_prompts = retain_prompts
        self._dirs = {
            "scoping": artifact_root / "scoping",
            "feasibility": artifact_root / "feasibility",
            "plans": artifact_root / "plans",
            "results": artifact_root / "results",
            "reviews": artifact_root / "reviews",
            "adjudications": artifact_root / "adjudications",
            "prompts": artifact_root / "prompts",
            "approvals": artifact_root / "approvals",
            "feedback": artifact_root / "feedback",
            "executions": artifact_root / "executions",
        }
        for directory in self._dirs.values():
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def artifact_root(self) -> Path:
        return self._artifact_root

    def save_plan(self, run_id: str, payload: dict[str, Any]) -> str:
        return self._write_versioned_json("plans", f"plan-{run_id[:8]}", payload)

    def save_feasibility(self, run_id: str, payload: dict[str, Any]) -> str:
        return self._write_versioned_json("feasibility", f"feasibility-{run_id[:8]}", payload)

    def save_step_result(self, run_id: str, step_number: int, payload: dict[str, Any]) -> str:
        self.clear_pending_step_result(step_number)
        return self._write_versioned_json("results", f"step-{step_number}-{run_id[:8]}", payload)

    def save_execution_result(self, run_id: str, payload: dict[str, Any]) -> str:
        self.clear_pending_execution_result(run_id)
        return self._write_versioned_json("results", f"execution-{run_id[:8]}", payload)

    def save_review(self, run_id: str, payload: dict[str, Any]) -> str:
        return self._write_versioned_json("reviews", f"review-{run_id[:8]}", payload)

    def save_adjudication(self, run_id: str, payload: dict[str, Any]) -> str:
        return self._write_versioned_json("adjudications", f"adj-{run_id[:8]}", payload)

    def save_scope_md(self, run_id: str, content: str) -> str:
        relative = Path("scoping") / f"scope-{run_id[:8]}.md"
        self._write_text(relative, content)
        return relative.as_posix()

    def save_claude_scope(self, run_id: str, round_num: int, content: str) -> str:
        return self._write_versioned_text(
            "scoping",
            f"claude-scope-r{round_num}-{run_id[:8]}",
            content,
        )

    def save_codex_scope(self, run_id: str, round_num: int, content: str) -> str:
        return self._write_versioned_text(
            "scoping",
            f"codex-scope-r{round_num}-{run_id[:8]}",
            content,
        )

    def save_debate_round(self, run_id: str, round_number: int, payload: dict[str, Any]) -> str:
        return self._write_versioned_json(
            "adjudications",
            f"debate-round-{round_number}-{run_id[:8]}",
            payload,
        )

    def save_execution_history(self, run_id: str, content: str) -> str:
        return self._write_versioned_text(
            "executions",
            f"execution-history-{run_id[:8]}",
            content,
        )

    def read_json(self, reference: str) -> dict[str, Any]:
        path = self._artifact_root / reference
        if not path.exists():
            raise ArtifactError(f"Artifact does not exist: {reference}")
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"Failed to read artifact: {reference}") from exc

    def read_text(self, reference: str) -> str:
        path = self._artifact_root / reference
        if not path.exists():
            raise ArtifactError(f"Artifact does not exist: {reference}")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ArtifactError(f"Failed to read artifact: {reference}") from exc

    def save_prompt(self, name: str, prompt: str) -> str | None:
        if not self._retain_prompts:
            return None
        relative = Path("prompts") / name
        self._write_text(relative, prompt)
        return relative.as_posix()

    def pending_step_result_path(self, step_number: int) -> Path:
        return self._dirs["results"] / f"pending-step-{step_number}.json"

    def clear_pending_step_result(self, step_number: int) -> None:
        path = self.pending_step_result_path(step_number)
        if path.exists():
            path.unlink()

    def pending_execution_result_path(self, run_id: str) -> Path:
        return self._dirs["results"] / f"pending-execution-{run_id[:8]}.json"

    def clear_pending_execution_result(self, run_id: str) -> None:
        path = self.pending_execution_result_path(run_id)
        if path.exists():
            path.unlink()

    def save_approval_decision(
        self,
        run_id: str,
        gate: str,
        decision: str,
        *,
        reason: str | None = None,
        force: bool = False,
    ) -> None:
        self._write_current_json(
            self._approval_pending_path(run_id, gate),
            {
                "run_id": run_id,
                "gate": gate,
                "decision": decision,
                "reason": reason,
                "force": force,
            },
        )

    def consume_approval_decision(self, run_id: str, gate: str) -> dict[str, Any] | None:
        pending = self._approval_pending_path(run_id, gate)
        if not pending.exists():
            return None
        data = self._read_path_json(pending)
        processed = self._approval_processed_path(run_id, gate)
        processed.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pending, processed)
        return data

    def latest_processed_approval(self, run_id: str, gate: str) -> dict[str, Any] | None:
        path = self._approval_processed_path(run_id, gate)
        if not path.exists():
            return None
        return self._read_path_json(path)

    def clear_processed_approval(self, run_id: str, gate: str) -> None:
        path = self._approval_processed_path(run_id, gate)
        if path.exists():
            path.unlink()

    def save_feedback(self, run_id: str, phase: str, reason: str) -> None:
        self._write_current_json(
            self._feedback_path(run_id, phase),
            {"run_id": run_id, "phase": phase, "reason": reason},
        )

    def load_feedback(self, run_id: str, phase: str) -> str | None:
        path = self._feedback_path(run_id, phase)
        if not path.exists():
            return None
        return str(self._read_path_json(path).get("reason") or "")

    def clear_feedback(self, run_id: str, phase: str) -> None:
        path = self._feedback_path(run_id, phase)
        if path.exists():
            path.unlink()

    def save_execution_manifest(self, run_id: str, payload: dict[str, Any]) -> None:
        self._write_current_json(self._execution_manifest_path(run_id), payload)

    def load_execution_manifest(self, run_id: str) -> dict[str, Any] | None:
        path = self._execution_manifest_path(run_id)
        if not path.exists():
            return None
        return self._read_path_json(path)

    def clear_execution_manifest(self, run_id: str) -> None:
        path = self._execution_manifest_path(run_id)
        if path.exists():
            path.unlink()

    def list_run_artifacts(self, run_id: str) -> list[Path]:
        matches: list[Path] = []
        for directory in self._dirs.values():
            if not directory.exists():
                continue
            matches.extend(sorted(directory.glob(f"*{run_id[:8]}*")))
        return matches

    def orphaned_worktrees(self, live_run_ids: set[str]) -> list[Path]:
        worktrees_dir = self._artifact_root / "worktrees"
        if not worktrees_dir.exists():
            return []
        orphans: list[Path] = []
        for path in sorted(worktrees_dir.iterdir()):
            if not path.is_dir() or not path.name.startswith("run-"):
                continue
            short_id = path.name[4:]
            if all(not run_id.startswith(short_id) for run_id in live_run_ids):
                orphans.append(path)
        return orphans

    def _approval_pending_path(self, run_id: str, gate: str) -> Path:
        return self._dirs["approvals"] / f"{run_id}-{gate}-pending.json"

    def _approval_processed_path(self, run_id: str, gate: str) -> Path:
        return self._dirs["approvals"] / f"{run_id}-{gate}-processed.json"

    def _feedback_path(self, run_id: str, phase: str) -> Path:
        return self._dirs["feedback"] / f"{run_id}-{phase}.json"

    def _execution_manifest_path(self, run_id: str) -> Path:
        return self._dirs["executions"] / f"run-{run_id}.json"

    def _write_versioned_json(self, bucket: str, prefix: str, payload: dict[str, Any]) -> str:
        relative = Path(bucket) / f"{prefix}-{uuid4().hex[:8]}.json"
        self._write_json(relative, payload)
        return relative.as_posix()

    def _write_versioned_text(
        self,
        bucket: str,
        prefix: str,
        content: str,
        *,
        ext: str = ".md",
    ) -> str:
        relative = Path(bucket) / f"{prefix}-{uuid4().hex[:8]}{ext}"
        self._write_text(relative, content)
        return relative.as_posix()

    def _write_current_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._write_json(path.relative_to(self._artifact_root), payload)

    def _write_json(self, relative: Path, payload: dict[str, Any]) -> None:
        path = self._artifact_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def _write_text(self, relative: Path, content: str) -> None:
        path = self._artifact_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, content)

    def _atomic_write(self, path: Path, content: str) -> None:
        try:
            fd, temp_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except OSError as exc:
            raise ArtifactError(f"Failed to write artifact: {path}") from exc

    def _read_path_json(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"Failed to read artifact: {path}") from exc
