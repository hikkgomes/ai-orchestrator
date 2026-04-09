"""Tests for state manager (src/ai_orchestrator/state.py).

Phase 2 from build-plan.md: state read/write/atomicity.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from ai_orchestrator.models import RunState, WorkflowStatus
from ai_orchestrator.state import StateError, StateManager


class TestStateManager:
    def test_save_and_load_roundtrip(self, artifact_root):
        """Saved state can be loaded back with identical values."""
        mgr = StateManager(artifact_root)
        state = RunState(run_id="abc123", task="Test task")
        state.status = WorkflowStatus.PLANNING
        mgr.save(state)
        loaded = mgr.load("abc123")
        assert loaded.run_id == "abc123"
        assert loaded.status == WorkflowStatus.PLANNING

    def test_atomic_write(self, artifact_root):
        """State file is written atomically (no partial writes visible)."""
        mgr = StateManager(artifact_root)
        state = RunState(run_id="atomic", task="Atomic")
        mgr.save(state)
        path = artifact_root / "state" / "run-atomic.json"
        payload = json.loads(path.read_text())
        assert payload["run_id"] == "atomic"
        assert not list((artifact_root / "state").glob("*.tmp"))

    def test_load_nonexistent_raises(self, artifact_root):
        """Loading a non-existent run_id raises StateError."""
        mgr = StateManager(artifact_root)
        with pytest.raises(StateError):
            mgr.load("does-not-exist")

    def test_exists(self, artifact_root):
        """exists() returns False before save and True after."""
        mgr = StateManager(artifact_root)
        assert not mgr.exists("xyz")
        state = RunState(run_id="xyz", task="t")
        mgr.save(state)
        assert mgr.exists("xyz")

    def test_list_runs(self, artifact_root):
        mgr = StateManager(artifact_root)
        mgr.save(RunState(run_id="one", task="1"))
        mgr.save(RunState(run_id="two", task="2"))
        assert mgr.list_runs() == ["one", "two"]

    def test_save_persists_run_metadata_in_sqlite(self, artifact_root):
        mgr = StateManager(artifact_root)
        state = RunState(run_id="meta", task="Track me")
        state.status = WorkflowStatus.EXECUTING
        mgr.save(state)

        conn = sqlite3.connect(artifact_root / "metadata.sqlite3")
        row = conn.execute(
            "SELECT run_id, task, status, current_phase FROM runs WHERE run_id = ?",
            ("meta",),
        ).fetchone()
        conn.close()

        assert row == ("meta", "Track me", "EXECUTING", "INIT")
        assert (artifact_root / "logs" / "run-meta.log").exists()
