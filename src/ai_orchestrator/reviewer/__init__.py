"""Embedded reviewer heuristics and repository-analysis helpers."""

from __future__ import annotations

from dataclasses import asdict
import importlib.resources
import json
from pathlib import Path
from typing import Any

from .scanner import scan_repository


def _bundled_rules_text() -> str:
    package = importlib.resources.files("ai_orchestrator.reviewer")
    return (package / "rules.yaml").read_text(encoding="utf-8")


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the bundled rules file without a PyYAML dependency.

    The bundled YAML intentionally uses only top-level keys with either a list of
    scalars or a flat mapping of scalars, which keeps the parser small and
    deterministic.
    """

    result: dict[str, Any] = {}
    current_key: str | None = None
    current_mode: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        if not raw_line.startswith(" "):
            key, _, remainder = line.partition(":")
            current_key = key.strip()
            remainder = remainder.strip()
            if remainder:
                result[current_key] = _parse_scalar(remainder)
                current_mode = None
            else:
                result[current_key] = {}
                current_mode = None
            continue

        if current_key is None:
            continue

        stripped = line.strip()
        if stripped.startswith("- "):
            if not isinstance(result[current_key], list):
                result[current_key] = []
            result[current_key].append(_parse_scalar(stripped[2:].strip()))
            current_mode = "list"
            continue

        nested_key, _, nested_value = stripped.partition(":")
        nested_key = nested_key.strip()
        nested_value = nested_value.strip()
        if current_mode != "mapping" or not isinstance(result[current_key], dict):
            result[current_key] = {}
        result[current_key][nested_key] = _parse_scalar(nested_value)
        current_mode = "mapping"

    return result


def load_rules() -> dict[str, Any]:
    """Load bundled reviewer rules."""

    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return _parse_simple_yaml(_bundled_rules_text())
    return yaml.safe_load(_bundled_rules_text()) or {}


def load_config(root: Path) -> dict[str, Any] | None:
    """Load ``.ai-review/config.json`` from *root* when present."""

    path = root / ".ai-review" / "config.json"
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


__all__ = ["load_config", "load_rules", "run_review_scan"]
