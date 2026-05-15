"""Embedded reviewer heuristics and repository-analysis helpers."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from ..paths import get_project_review_dir
from .scanner import scan_repository

def load_config(root: Path) -> dict[str, Any] | None:
    """Load reviewer config from centralized project storage when present."""

    path = get_project_review_dir(root) / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_review_scan(
    root: Path,
    *,
    changed_files: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the heuristic scanner and return JSON-serialisable findings."""

    findings = scan_repository(root, files=changed_files, config=config)
    return [asdict(finding) for finding in findings]


__all__ = ["load_config", "run_review_scan"]
