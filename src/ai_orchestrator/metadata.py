"""SQLite-backed metadata persistence for runs and adapter invocations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .models import RunState


@dataclass
class InvocationRecord:
    """Metadata captured for a single adapter invocation."""

    cli_name: str
    command: list[str]
    working_dir: str
    timeout_seconds: int
    exit_code: int | None
    started_at: str
    finished_at: str
    stdout: str
    stderr: str
    raw_log_path: str | None = None
    run_id: str | None = None
    step_number: int | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    output_source: str | None = None
    summary: str | None = None
    status: str | None = None
    issues: list[str] | None = None
    test_commands: list[str] | None = None


class MetadataStore:
    """Persist run and invocation metadata in a local SQLite database."""

    def __init__(self, artifact_root: Path) -> None:
        self._db_path = artifact_root / "metadata.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'default',
                    status TEXT NOT NULL,
                    current_phase TEXT NOT NULL,
                    plan_id TEXT,
                    review_id TEXT,
                    rework_count INTEGER NOT NULL,
                    replan_count INTEGER NOT NULL,
                    retry_counts_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    base_commit TEXT NOT NULL,
                    worktree_path TEXT,
                    worktree_branch TEXT
                );

                CREATE TABLE IF NOT EXISTS invocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    step_number INTEGER,
                    cli_name TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    working_dir TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    exit_code INTEGER,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    stdout_chars INTEGER NOT NULL,
                    stderr_chars INTEGER NOT NULL,
                    raw_log_path TEXT,
                    model TEXT,
                    reasoning_effort TEXT,
                    output_source TEXT,
                    result_status TEXT,
                    summary TEXT,
                    issues_json TEXT NOT NULL,
                    test_commands_json TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "mode" not in columns:
                conn.execute("ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'default'")

    def upsert_run(self, state: RunState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task, mode, status, current_phase, plan_id, review_id,
                    rework_count, replan_count, retry_counts_json,
                    created_at, updated_at, error, base_commit, worktree_path,
                    worktree_branch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    task = excluded.task,
                    mode = excluded.mode,
                    status = excluded.status,
                    current_phase = excluded.current_phase,
                    plan_id = excluded.plan_id,
                    review_id = excluded.review_id,
                    rework_count = excluded.rework_count,
                    replan_count = excluded.replan_count,
                    retry_counts_json = excluded.retry_counts_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    error = excluded.error,
                    base_commit = excluded.base_commit,
                    worktree_path = excluded.worktree_path,
                    worktree_branch = excluded.worktree_branch
                """,
                (
                    state.run_id,
                    state.task,
                    state.mode,
                    state.status,
                    state.current_phase,
                    state.plan_id,
                    state.review_id,
                    state.rework_count,
                    state.replan_count,
                    json.dumps(state.retry_counts, sort_keys=True),
                    state.created_at,
                    state.updated_at,
                    state.error,
                    state.base_commit,
                    state.worktree_path,
                    state.worktree_branch,
                ),
            )

    def record_invocation(self, record: InvocationRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO invocations (
                    run_id, step_number, cli_name, command_json, working_dir,
                    timeout_seconds, exit_code, started_at, finished_at,
                    stdout_chars, stderr_chars, raw_log_path, model,
                    reasoning_effort, output_source, result_status, summary,
                    issues_json, test_commands_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.step_number,
                    record.cli_name,
                    json.dumps(record.command),
                    record.working_dir,
                    record.timeout_seconds,
                    record.exit_code,
                    record.started_at,
                    record.finished_at,
                    len(record.stdout),
                    len(record.stderr),
                    record.raw_log_path,
                    record.model,
                    record.reasoning_effort,
                    record.output_source,
                    record.status,
                    record.summary,
                    json.dumps(record.issues or []),
                    json.dumps(record.test_commands or []),
                ),
            )
