"""Abstract base adapter interface and shared error hierarchy.

All CLI adapters (ClaudeAdapter, CodexAdapter) implement ``BaseAdapter``.

Error hierarchy::

    AdapterError
    ├── StepFailure   — generic execution failure; retry if under limit
    └── BlockedOnCLI  — CLI needs interactive input / auth refresh; no retry

See AGENTS.md for the full adapter contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4

from ..metadata import InvocationRecord, MetadataStore
from ..models import StepResult
from ..validator import ValidationError, Validator, validate_schema


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    """Base class for all adapter errors."""


class StepFailure(AdapterError):
    """CLI invocation failed; retry is permitted if under the retry limit.

    Parameters
    ----------
    exit_code:
        Process exit code (None if killed by timeout).
    stdout:
        Captured standard output.
    stderr:
        Captured standard error.
    validation_error:
        Schema or application validation error message, if applicable.
    """

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        validation_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.validation_error = validation_error


class BlockedOnCLI(AdapterError):
    """CLI requires interactive input or auth refresh.

    No retry.  The engine transitions to ``BLOCKED_ON_CLI`` state so the user
    can fix the issue and then ``aio resume``.

    Parameters
    ----------
    exit_code:
        Process exit code.
    stderr:
        Captured standard error (may contain auth error messages).
    """

    def __init__(
        self,
        message: str,
        exit_code: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass
class CommandResult:
    """Normalized subprocess execution result."""

    stdout: str
    stderr: str
    exit_code: int | None
    started_at: str
    finished_at: str
    raw_log_path: Path | None
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Base adapter interface
# ---------------------------------------------------------------------------


class BaseAdapter(ABC):
    """Abstract interface that all CLI adapters must implement.

    Parameters
    ----------
    config:
        Adapter-relevant section of the orchestrator config.
    artifact_root:
        Path to the ``.ai-orchestrator/`` directory for log writing.
    """

    def __init__(self, config: Any, artifact_root: Path) -> None:
        self._config = config
        self._artifact_root = artifact_root
        self._metadata = MetadataStore(artifact_root)
        self._timeout_grace_period = 10

    @abstractmethod
    def invoke(
        self,
        prompt: str,
        working_dir: Path,
        timeout: int,
        schema: dict[str, Any],
        *,
        step_number: int | None = None,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """Invoke the CLI with *prompt* and return a validated result dict.

        Parameters
        ----------
        prompt:
            Fully rendered prompt string (task + context + schema).
        working_dir:
            Absolute path to run the subprocess in (repo root or worktree).
        timeout:
            Seconds before the subprocess is killed.
        schema:
            JSON schema dict to validate the output against.
        step_number:
            Optional step number context for execution-phase validations.
        reasoning_effort_override:
            Optional phase-specific reasoning effort override.
        model_override:
            Optional phase-specific model override.

        Returns
        -------
        dict
            Validated JSON output from the CLI.

        Raises
        ------
        StepFailure
            Generic execution failure.  Retry is permitted.
        BlockedOnCLI
            CLI needs interactive input or auth.  Do not retry.
        """

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_env(env: dict[str, str] | None = None) -> dict[str, str]:
        """Return a subprocess environment with only allowed variables.

        Allowlist: PATH, HOME, USER, LANG, TERM, GIT_DIR, GIT_WORK_TREE.
        All credential/secret variables are stripped.
        """
        import os

        allowed = {"PATH", "HOME", "USER", "LANG", "TERM", "GIT_DIR", "GIT_WORK_TREE"}
        source = env if env is not None else os.environ
        return {k: v for k, v in source.items() if k in allowed}

    def _write_raw_output(
        self,
        cli_name: str,
        command: list[str],
        stdout: str,
        stderr: str,
        exit_code: int | None,
        started_at: str,
        finished_at: str,
    ) -> Path | None:
        if not getattr(self._config.logging, "retain_raw_output", False):
            return None

        log_dir = self._artifact_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{cli_name}-{uuid4().hex[:8]}.log"
        payload = {
            "started_at": started_at,
            "finished_at": finished_at,
            "command": command,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return path

    def _validate_output(
        self,
        data: dict[str, Any],
        schema: dict[str, Any],
        working_dir: Path,
        *,
        step_number: int | None = None,
    ) -> dict[str, Any]:
        validator = Validator(working_dir)
        title = schema.get("title")
        try:
            if title == "TaskDefinition":
                return validator.validate_scoping(data)
            if title == "Plan":
                return validator.validate_plan(data)
            if title == "StepResult":
                if step_number is None:
                    step_number = int(data.get("step_number", 0))
                return validator.validate_step_result(data, step_number)
            if title == "FeasibilityResult":
                return validator.validate_feasibility(data)
            if title == "Review":
                return validator.validate_review(data)
            if title == "Adjudication":
                return validator.validate_adjudication(data)
            validate_schema(data, schema)
        except ValidationError as exc:
            detail = exc.detail or str(exc)
            raise StepFailure(
                "Schema validation failed",
                validation_error=detail,
            ) from exc

        return data

    def _run_subprocess(
        self,
        cli_name: str,
        command: list[str],
        working_dir: Path,
        timeout: int,
    ) -> CommandResult:
        started_at = self._now()
        process = subprocess.Popen(
            command,
            cwd=working_dir,
            env=self._filter_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        timed_out = False
        exit_code: int | None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                process.terminate()
            except OSError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=self._timeout_grace_period)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                stdout, stderr = process.communicate()
            exit_code = None

        finished_at = self._now()
        raw_log_path = self._write_raw_output(
            cli_name,
            command,
            stdout,
            stderr,
            exit_code,
            started_at,
            finished_at,
        )
        return CommandResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            raw_log_path=raw_log_path,
            timed_out=timed_out,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _record_invocation(self, record: InvocationRecord) -> None:
        self._metadata.record_invocation(record)

    @staticmethod
    def _typed_step_result(data: dict[str, Any]) -> StepResult | None:
        if data.get("step_number") is None:
            return None
        try:
            return StepResult.model_validate(data)
        except Exception:
            return None
