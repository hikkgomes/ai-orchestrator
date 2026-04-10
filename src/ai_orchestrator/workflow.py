"""Workflow definition loading from ``workflows/default.yaml``.

The repository uses a frozen, checked-in workflow definition as the
authoritative source for phase structure and default phase-level settings.
``aio.toml`` supplies user-level overrides for supported settings after the
definition is loaded. The YAML file is intentionally small, so a minimal parser
for the supported subset is sufficient and avoids adding a runtime YAML
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any


class WorkflowError(Exception):
    """Raised when the workflow definition cannot be loaded or is invalid."""


@dataclass
class WorkflowPhase:
    """Single phase entry from the workflow definition."""

    name: str
    cli: str | None = None
    approval_gate: str | None = None
    worktree: bool = False
    retries: int = 0
    loop_limits: dict[str, int] = field(default_factory=dict)
    pre_checks: list[str] = field(default_factory=list)


@dataclass
class WorkflowDefinition:
    """Loaded workflow definition."""

    name: str
    description: str
    phases: dict[str, WorkflowPhase]

    def phase(self, name: str) -> WorkflowPhase:
        try:
            return self.phases[name]
        except KeyError as exc:
            raise WorkflowError(f"Workflow phase '{name}' is not defined") from exc


def load_workflow_definition(repo_root: Path) -> WorkflowDefinition:
    """Load the canonical workflow definition from ``workflows/default.yaml``."""
    path = repo_root / "workflows" / "default.yaml"
    if not path.exists():
        raise WorkflowError(f"Workflow definition does not exist: {path}")

    data = _parse_supported_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WorkflowError("Workflow definition must be a mapping")

    phases_data = data.get("phases")
    if not isinstance(phases_data, dict) or not phases_data:
        raise WorkflowError("Workflow definition must contain a non-empty phases mapping")

    phases: dict[str, WorkflowPhase] = {}
    for phase_name, raw_phase in phases_data.items():
        if not isinstance(raw_phase, dict):
            raise WorkflowError(f"Workflow phase '{phase_name}' must be a mapping")
        phases[phase_name] = WorkflowPhase(
            name=phase_name,
            cli=_string_or_none(raw_phase.get("cli")),
            approval_gate=_string_or_none(raw_phase.get("approval_gate")),
            worktree=bool(raw_phase.get("worktree", False)),
            retries=int(raw_phase.get("retries", 0)),
            loop_limits=_int_mapping(raw_phase.get("loop_limits", {})),
            pre_checks=_string_list(raw_phase.get("pre_checks", [])),
        )

    return WorkflowDefinition(
        name=str(data.get("name", "default")),
        description=str(data.get("description", "")).strip(),
        phases=phases,
    )


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(item) for key, item in value.items()}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _parse_supported_yaml(text: str) -> Any:
    lines = text.splitlines()
    parsed, index = _parse_block(lines, 0, 0)
    index = _skip_ignored(lines, index)
    if index != len(lines):
        raise WorkflowError("Unexpected trailing content in workflow definition")
    return parsed


def _parse_block(lines: list[str], start: int, indent: int) -> tuple[Any, int]:
    index = _skip_ignored(lines, start)
    if index >= len(lines):
        return {}, index

    stripped = lines[index].lstrip(" ")
    if stripped.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[str], start: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    index = start
    while index < len(lines):
        raw = lines[index]
        if _is_ignored(raw):
            index += 1
            continue
        current_indent = _indent_of(raw)
        if current_indent < indent:
            break
        if current_indent != indent:
            raise WorkflowError(f"Unexpected indentation at line {index + 1}")

        content = raw[current_indent:]
        if content.startswith("- "):
            raise WorkflowError(f"Unexpected list item at line {index + 1}")
        if ":" not in content:
            raise WorkflowError(f"Expected key/value mapping at line {index + 1}")

        key, remainder = content.split(":", 1)
        key = key.strip()
        remainder = remainder.strip()

        if remainder == ">":
            value, index = _parse_folded_scalar(lines, index + 1, indent)
            result[key] = value
            continue

        if remainder:
            result[key] = _parse_scalar(remainder)
            index += 1
            continue

        next_index = _skip_ignored(lines, index + 1)
        if next_index >= len(lines) or _indent_of(lines[next_index]) <= indent:
            result[key] = {}
            index = next_index
            continue

        value, index = _parse_block(lines, next_index, indent + 2)
        result[key] = value

    return result, index


def _parse_list(lines: list[str], start: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    index = start
    while index < len(lines):
        raw = lines[index]
        if _is_ignored(raw):
            index += 1
            continue
        current_indent = _indent_of(raw)
        if current_indent < indent:
            break
        if current_indent != indent:
            raise WorkflowError(f"Unexpected indentation at line {index + 1}")

        content = raw[current_indent:]
        if not content.startswith("- "):
            break
        item_text = content[2:].strip()
        items.append(_parse_scalar(item_text))
        index += 1

    return items, index


def _parse_folded_scalar(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    parts: list[str] = []
    index = start
    while index < len(lines):
        raw = lines[index]
        if _is_ignored(raw):
            index += 1
            continue
        current_indent = _indent_of(raw)
        if current_indent <= parent_indent:
            break
        parts.append(raw.strip())
        index += 1
    return " ".join(parts).strip(), index


def _parse_scalar(value: str) -> Any:
    # Supported YAML scalar subset: booleans, quoted strings, integers, and simple floats.
    if value in {"true", "false"}:
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def _skip_ignored(lines: list[str], index: int) -> int:
    while index < len(lines) and _is_ignored(lines[index]):
        index += 1
    return index


def _is_ignored(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))
