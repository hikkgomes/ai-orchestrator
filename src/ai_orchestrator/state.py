"""Orchestrator run state persistence.

Reads and writes ``state/run-<uuid>.json`` using:
- ``filelock`` for cross-process mutual exclusion
- ``os.replace()`` for atomic file replacement (write to temp, then rename)

The ``RunState`` Pydantic model is defined in ``models.py``.

Usage::

    state_mgr = StateManager(artifact_root)
    state = state_mgr.load(run_id)
    state.status = WorkflowStatus.PLANNING
    state_mgr.save(state)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .event_log import EventLogger
from .metadata import MetadataStore
from .models import RunState


class StateError(Exception):
    """Raised when state cannot be read or written."""


class StateManager:
    """Manages atomic read/write of run state files.

    Parameters
    ----------
    artifact_root:
        Path to the centralized project artifact directory.
    """

    def __init__(self, artifact_root: Path) -> None:
        self._artifact_root = artifact_root
        self._state_dir = artifact_root / "state"
        self._metadata = MetadataStore(artifact_root)

    def _state_path(self, run_id: str) -> Path:
        return self._state_dir / f"run-{run_id}.json"

    def _lock_path(self, run_id: str) -> Path:
        return self._state_dir / f"run-{run_id}.lock"

    def load(self, run_id: str) -> RunState:
        """Load run state from disk.

        Parameters
        ----------
        run_id:
            UUID of the run to load.

        Returns
        -------
        RunState
            Parsed run state.

        Raises
        ------
        StateError
            If the state file does not exist or contains invalid JSON.
        """
        path = self._state_path(run_id)
        lock = FileLock(str(self._lock_path(run_id)))
        try:
            with lock:
                if not path.exists():
                    raise StateError(f"State file does not exist for run {run_id}")
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                except json.JSONDecodeError as exc:
                    raise StateError(f"State file for run {run_id} is invalid JSON") from exc
        except OSError as exc:
            raise StateError(f"Failed to load state for run {run_id}: {exc}") from exc

        try:
            return RunState.model_validate(payload)
        except Exception as exc:
            raise StateError(f"State file for run {run_id} is invalid: {exc}") from exc

    def save(self, state: RunState) -> None:
        """Atomically persist run state to disk.

        Parameters
        ----------
        state:
            The current run state to write.

        Raises
        ------
        StateError
            If the write fails.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_path(state.run_id)
        lock = FileLock(str(self._lock_path(state.run_id)))
        state.updated_at = datetime.now(timezone.utc).isoformat()
        payload = state.model_dump(mode="json", exclude_none=False)

        try:
            with lock:
                fd, temp_name = tempfile.mkstemp(
                    dir=self._state_dir,
                    prefix=f".run-{state.run_id}-",
                    suffix=".tmp",
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle, indent=2, sort_keys=True)
                        handle.write("\n")
                    os.replace(temp_name, path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
        except OSError as exc:
            raise StateError(f"Failed to save state for run {state.run_id}: {exc}") from exc

        self._metadata.upsert_run(state)
        EventLogger(self._artifact_root, state.run_id).log(
            "state_saved",
            run_id=state.run_id,
            status=state.status,
            current_phase=state.current_phase,
        )

    def exists(self, run_id: str) -> bool:
        """Return True if a state file exists for the given run_id."""
        return self._state_path(run_id).exists()

    def list_runs(self) -> list[str]:
        """Return run IDs for all state files present on disk."""
        if not self._state_dir.exists():
            return []
        run_ids = []
        for path in sorted(self._state_dir.glob("run-*.json")):
            name = path.stem
            if name.startswith("run-"):
                run_ids.append(name[4:])
        return run_ids

    def resolve_run_id(self, prefix: str | None) -> str:
        """Resolve *prefix* to a concrete run ID.

        Resolution rules:
        - ``None``, empty, or ``"latest"`` resolves to the most recently updated run
        - exact matches return immediately
        - unique prefixes are expanded to the full run ID

        Raises
        ------
        StateError
            If there are no runs, no matches, or multiple matching prefixes.
        """
        normalized = (prefix or "").strip()
        run_ids = self.list_runs()
        if not run_ids:
            raise StateError("No runs found.")

        if not normalized or normalized == "latest":
            latest_path = max(self._state_dir.glob("run-*.json"), key=lambda path: path.stat().st_mtime_ns)
            return latest_path.stem[4:]

        if normalized in run_ids:
            return normalized

        matches = [run_id for run_id in run_ids if run_id.startswith(normalized)]
        if not matches:
            raise StateError(f"No run matches '{normalized}'.")
        if len(matches) == 1:
            return matches[0]

        preview = ", ".join(match[:8] for match in matches[:5])
        suffix = "..." if len(matches) > 5 else ""
        raise StateError(f"Run ID prefix '{normalized}' is ambiguous: {preview}{suffix}")

    def latest_run_timestamp(self) -> str | None:
        if not self._state_dir.exists():
            return None
        latest_path = max(self._state_dir.glob("run-*.json"), key=lambda path: path.stat().st_mtime_ns, default=None)
        if latest_path is None:
            return None
        try:
            state = self.load(latest_path.stem[4:])
        except StateError:
            return None
        return state.updated_at or state.created_at
