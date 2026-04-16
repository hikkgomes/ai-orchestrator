"""JSON schema + application-level validation for all workflow artifacts.

Two validation layers (docs/design-decisions.md DD-8):

1. **Schema validation** — structural check via ``jsonschema``.  The schemas
   live in ``src/ai_orchestrator/schemas/`` (bundled) and ``schemas/`` (source).

2. **Application validation** — semantic invariants that JSON Schema cannot
   express:
   - Path normalisation: reject any path containing ``..`` segments, or
     starting with ``/``, after resolving against the repo root
   - ``files_changed`` correspondence with ``git diff`` (Codex results only)
   - Review conditional field requirements
   - Adjudication conditional field requirements

Usage::

    validator = Validator(repo_root)
    validated_plan = validator.validate_plan(raw_dict)
    validated_result = validator.validate_execution_result(raw_dict)
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from typing import Any

import jsonschema


class ValidationError(Exception):
    """Raised when schema or application-level validation fails."""

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


def _load_schema(name: str) -> dict[str, Any]:
    """Load a bundled JSON schema by filename (e.g. 'plan.schema.json')."""
    package = importlib.resources.files("ai_orchestrator.schemas")
    with (package / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_bundled_schema(name: str) -> dict[str, Any]:
    """Public wrapper for loading a bundled schema by filename."""
    return _load_schema(name)


def _validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = sorted(validator.iter_errors(data), key=lambda err: list(err.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path)
        detail = f"{location}: {first.message}" if location else first.message
        raise ValidationError("Schema validation failed", detail)


def validate_schema(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate *data* against an arbitrary JSON schema."""
    _validate_schema(data, schema)


def _validate_no_path_traversal(paths: list[str]) -> None:
    """Raise ValidationError if any path contains ``..`` or starts with ``/``.

    Checks both leading ``/`` and any embedded ``../`` segment per DD-8.
    """
    for path_str in paths:
        path = Path(path_str)
        if path.is_absolute():
            raise ValidationError("Unsafe path", f"Absolute path is not allowed: {path_str}")
        if ".." in path.parts:
            raise ValidationError("Unsafe path", f"Path traversal is not allowed: {path_str}")


def _validate_paths_confined(repo_root: Path, paths: list[str]) -> None:
    _validate_no_path_traversal(paths)
    for path_str in paths:
        resolved = (repo_root / path_str).resolve()
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise ValidationError(
                "Unsafe path",
                f"Path escapes repository root: {path_str}",
            ) from exc


class Validator:
    """Validates workflow artifacts against schemas and application invariants.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root, used for path confinement checks.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root.resolve()
        self._schemas: dict[str, Any] = {}

    def _get_schema(self, name: str) -> dict[str, Any]:
        if name not in self._schemas:
            self._schemas[name] = _load_schema(name)
        return self._schemas[name]

    def validate_plan(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a plan artifact (schema + application level).

        Application checks:
        - ``implementation_steps`` is non-empty
        - All flat ``key_files`` paths are safe

        Returns the original *data* dict on success.

        Raises
        ------
        ValidationError
        """
        _validate_schema(data, self._get_schema("plan.schema.json"))
        if not data.get("implementation_steps"):
            raise ValidationError(
                "Invalid plan",
                "Plans require at least one implementation step",
            )

        _validate_paths_confined(self._repo_root, data.get("key_files", []))

        return data

    def validate_scoping(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a scoping result artifact."""
        if "actionable" not in data:
            data = {
                **data,
                "actionable": False if data.get("blocking_reason") else True,
            }
        if "assumptions" not in data:
            data = {**data, "assumptions": []}

        _validate_schema(data, self._get_schema("scoping.schema.json"))

        if data.get("actionable") is False and not data.get("blocking_reason"):
            raise ValidationError(
                "Invalid scoping result",
                "Non-actionable scoping results require blocking_reason",
            )

        return data

    def validate_step_result(
        self,
        data: dict[str, Any],
        step_number: int,
    ) -> dict[str, Any]:
        """Validate a step result artifact (schema + application level).

        Application checks:
        - ``step_number`` matches the expected *step_number*
        - All ``files_changed`` paths are safe
        - If ``status == "success"``, ``files_changed`` must be non-empty

        Returns the original *data* dict on success.

        Raises
        ------
        ValidationError
        """
        _validate_schema(data, self._get_schema("step_result.schema.json"))

        if data["step_number"] != step_number:
            raise ValidationError(
                "Invalid step result",
                f"Expected step_number {step_number}, got {data['step_number']}",
            )

        _validate_paths_confined(
            self._repo_root,
            [change["path"] for change in data.get("files_changed", [])],
        )

        if data["status"] == "success" and not data.get("files_changed"):
            raise ValidationError(
                "Invalid step result",
                "Successful step results must include at least one changed file",
            )

        workspace_diffs = data.get("workspace_diffs") or {}
        if not isinstance(workspace_diffs, dict):
            raise ValidationError("Invalid step result", "workspace_diffs must be an object")

        return data

    def validate_execution_result(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a full execution result artifact."""
        _validate_schema(data, self._get_schema("execution_result.schema.json"))

        _validate_paths_confined(
            self._repo_root,
            [change["path"] for change in data.get("files_changed", [])],
        )

        if data["status"] == "success" and not data.get("files_changed"):
            raise ValidationError(
                "Invalid execution result",
                "Successful execution results must include at least one changed file",
            )

        workspace_diffs = data.get("workspace_diffs") or {}
        if not isinstance(workspace_diffs, dict):
            raise ValidationError("Invalid execution result", "workspace_diffs must be an object")

        return data

    def validate_review(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a review artifact (schema + application level).

        Application checks:
        - ``score`` is between 1 and 10
        - If ``verdict == "reject"``, ``blocks_merge`` must be True
        - If verdict is ``reject`` or ``request_changes``, at least one
          finding with severity ``critical`` or ``major`` must exist

        Returns the original *data* dict on success.

        Raises
        ------
        ValidationError
        """
        _validate_schema(data, self._get_schema("review.schema.json"))

        verdict = data["verdict"]
        findings = data.get("findings", [])
        if verdict == "reject" and data.get("blocks_merge") is not True:
            raise ValidationError(
                "Invalid review",
                "Rejected reviews must set blocks_merge to true",
            )
        if verdict in {"reject", "request_changes"}:
            has_blocking = any(
                finding.get("severity") in {"critical", "major"} for finding in findings
            )
            if not has_blocking:
                raise ValidationError(
                    "Invalid review",
                    "Blocking reviews require at least one critical or major finding",
                )

        return data

    def validate_feasibility(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate a feasibility result artifact."""
        _validate_schema(data, self._get_schema("feasibility.schema.json"))

        if data["verdict"] == "blocked":
            has_critical = any(
                issue.get("severity") == "critical" for issue in data.get("blocking_issues", [])
            )
            if not has_critical:
                raise ValidationError(
                    "Invalid feasibility result",
                    "Blocked feasibility results require at least one critical issue",
                )

        return data

    def validate_adjudication(
        self,
        data: dict[str, Any],
        *,
        plan_step_numbers: set[int] | None = None,
    ) -> dict[str, Any]:
        """Validate an adjudication artifact (schema + application level).

        Application checks:
        - If ``verdict == "REWORK"``: ``rework_feedback`` present
        - If ``verdict == "REPLAN"``: ``replan_feedback`` present
        - If ``verdict == "FAIL"``: ``failure_reason`` present

        Returns the original *data* dict on success.

        Raises
        ------
        ValidationError
        """
        _validate_schema(data, self._get_schema("adjudication.schema.json"))

        verdict = data["verdict"]
        if verdict == "REWORK":
            if not data.get("rework_feedback"):
                raise ValidationError(
                    "Invalid adjudication",
                    "REWORK requires rework_feedback",
                )
        if verdict == "REPLAN" and not data.get("replan_feedback"):
            raise ValidationError(
                "Invalid adjudication",
                "REPLAN requires replan_feedback",
            )
        if verdict == "FAIL" and not data.get("failure_reason"):
            raise ValidationError(
                "Invalid adjudication",
                "FAIL requires failure_reason",
            )

        return data
