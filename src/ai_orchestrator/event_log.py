"""Structured event logging helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventLogger:
    """Write JSONL event records to ``logs/run-<uuid>.log``."""

    def __init__(self, artifact_root: Path, run_id: str) -> None:
        self._path = artifact_root / "logs" / f"run-{run_id}.log"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
