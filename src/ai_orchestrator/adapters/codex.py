"""Codex CLI adapter.

Invokes ``codex exec "<prompt>"`` as a subprocess per AGENTS.md.

Three-tier output strategy:
1. **Result file (primary)**: prompt instructs Codex to write a JSON result to
   ``.ai-orchestrator/results/pending-step-<n>.json``.
2. **Stdout fallback**: scan stdout from the end for the last valid JSON object.
3. **Git-diff-only fallback**: reconstruct a minimal ``step_result`` from
   ``git diff --name-status`` in the worktree.

In all cases, ``files_changed`` is verified against ``git diff`` — the git diff
is ground truth; the AI-provided list is metadata only.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ..metadata import InvocationRecord
from .base import BaseAdapter, BlockedOnCLI, StepFailure


_AUTH_PATTERNS = (
    "authentication required",
    "login required",
    "not authenticated",
    "please log in",
    "interactive",
    "auth expired",
)

_STEP_RESULT_PATH_PATTERN = re.compile(r"pending-step-(\d+)\.json")
_RESULT_FILE_PATH_PATTERN = re.compile(
    r"(?:write your result JSON to:|write a JSON result file to the path:)\s*\n([^\n]+\.json)",
    re.IGNORECASE,
)
_ANY_JSON_PATH_PATTERN = re.compile(r"([^\s]+\.json)")


class CodexAdapter(BaseAdapter):
    """Adapter for the ``codex exec`` CLI.

    See AGENTS.md → Codex Adapter for the full contract.
    """

    CLI_NAME = "codex"
    _MODEL_FLAG = "--model"

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
        max_turns_override: int | None = None,
    ) -> dict[str, Any]:
        """Invoke ``codex exec`` and return a validated output dict.

        Raises
        ------
        StepFailure
            On non-zero exit or output parsing/validation failure.
        BlockedOnCLI
            On auth/interactive exit or timeout with no output.
        """
        schema_title = schema.get("title")
        result_path = self._extract_result_path(prompt, working_dir)
        if schema_title == "StepResult":
            step_number = step_number or self._extract_step_number(prompt)
            if step_number is None:
                raise StepFailure("Codex prompt is missing the pending step result path")
            result_path = self._pending_result_path(step_number)

        command, model, reasoning_effort = self._build_command(
            prompt,
            reasoning_effort_override=reasoning_effort_override,
            model_override=model_override,
        )
        completed = self._run_subprocess(self.CLI_NAME, command, working_dir, timeout)
        stdout = completed.stdout
        stderr = completed.stderr
        raw_log_path = completed.raw_log_path
        started_at = completed.started_at
        finished_at = completed.finished_at

        if completed.timed_out:
            self._record_invocation(
                InvocationRecord(
                    cli_name=self.CLI_NAME,
                    command=command,
                    working_dir=str(working_dir),
                    timeout_seconds=timeout,
                    exit_code=None,
                    started_at=started_at,
                    finished_at=finished_at,
                    stdout=stdout,
                    stderr=stderr,
                    raw_log_path=str(raw_log_path) if raw_log_path else None,
                    step_number=step_number,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
            )
            if not stdout.strip() and not stderr.strip():
                raise BlockedOnCLI(
                    "Codex CLI timed out without producing output",
                    exit_code=None,
                    stderr=stderr,
                )
            raise StepFailure(
                "Codex CLI timed out",
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
            )

        exit_code = completed.exit_code or 0
        if exit_code != 0:
            self._record_invocation(
                InvocationRecord(
                    cli_name=self.CLI_NAME,
                    command=command,
                    working_dir=str(working_dir),
                    timeout_seconds=timeout,
                    exit_code=exit_code,
                    started_at=started_at,
                    finished_at=finished_at,
                    stdout=stdout,
                    stderr=stderr,
                    raw_log_path=str(raw_log_path) if raw_log_path else None,
                    step_number=step_number,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
            )
            if self._is_auth_error(stderr):
                raise BlockedOnCLI(
                    "Codex CLI requires interactive action",
                    exit_code=exit_code,
                    stderr=stderr,
                )
            raise StepFailure(
                "Codex CLI exited with a non-zero status",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )

        output_source = "stdout"
        result = self._try_result_file(result_path) if result_path else None
        if result is not None:
            output_source = "result-file"
        else:
            result = self._scan_stdout_for_json(stdout)

        if schema_title == "StepResult":
            try:
                git_result = self._git_diff_fallback(working_dir, step_number or 0)
            except StepFailure:
                if result is None or (working_dir / ".git").exists():
                    raise
                validated = self._validate_output(
                    result,
                    schema,
                    working_dir,
                    step_number=step_number or result.get("step_number"),
                )
            else:
                if result is None:
                    output_source = "git-diff"
                merged = self._merge_result_metadata(
                    git_result,
                    result,
                    step_number=step_number or 0,
                )
                validated = self._validate_output(
                    merged,
                    schema,
                    working_dir,
                    step_number=step_number or merged.get("step_number"),
                )
        else:
            if result is None:
                raise StepFailure(
                    "Codex CLI did not produce a parseable JSON result",
                    stdout=stdout,
                    stderr=stderr,
                )
            validated = self._validate_output(result, schema, working_dir)
        typed_result = self._typed_step_result(validated)
        self._record_invocation(
            InvocationRecord(
                cli_name=self.CLI_NAME,
                command=command,
                working_dir=str(working_dir),
                timeout_seconds=timeout,
                exit_code=exit_code,
                started_at=started_at,
                finished_at=finished_at,
                stdout=stdout,
                stderr=stderr,
                raw_log_path=str(raw_log_path) if raw_log_path else None,
                step_number=step_number,
                model=model,
                reasoning_effort=reasoning_effort,
                output_source=output_source,
                summary=typed_result.summary if typed_result else None,
                status=typed_result.status.value if typed_result else None,
                issues=typed_result.issues if typed_result else None,
                test_commands=typed_result.test_commands if typed_result else None,
            )
        )
        return validated

    def _build_command(
        self,
        prompt: str,
        *,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
    ) -> tuple[list[str], str | None, str | None]:
        command = [self.CLI_NAME, "exec"]
        model = model_override or getattr(self._config.routing.codex, "model", "") or None
        reasoning_effort = (
            reasoning_effort_override
            or getattr(self._config.routing.codex, "reasoning_effort", "")
            or None
        )
        if model:
            command.extend([self._MODEL_FLAG, model])
        if reasoning_effort:
            command.extend(["--config", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
        command.append(prompt)
        return command, model, reasoning_effort

    def _pending_result_path(self, step_number: int) -> Path:
        """Return the expected result file path for *step_number*."""
        return self._artifact_root / "results" / f"pending-step-{step_number}.json"

    @staticmethod
    def _extract_step_number(prompt: str) -> int | None:
        match = _STEP_RESULT_PATH_PATTERN.search(prompt)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_result_path(prompt: str, working_dir: Path) -> Path | None:
        match = _RESULT_FILE_PATH_PATTERN.search(prompt)
        if not match:
            match = _ANY_JSON_PATH_PATTERN.search(prompt)
        if not match:
            return None
        path = Path(match.group(1).strip())
        if not path.is_absolute():
            path = working_dir / path
        return path

    def _try_result_file(self, path: Path) -> dict[str, Any] | None:
        """Read and parse the result file if it exists; return None otherwise."""
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _scan_stdout_for_json(stdout: str) -> dict[str, Any] | None:
        """Scan stdout from the end for the last valid JSON object.

        Returns the parsed dict or None if no valid JSON found.
        """
        stripped = stdout.strip()
        if not stripped:
            return None

        candidates: list[dict[str, Any]] = []
        for line in reversed(stripped.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                candidates.append(parsed)

        decoder = json.JSONDecoder()
        last_dict: dict[str, Any] | None = None
        for start in range(len(stripped)):
            if stripped[start] != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                last_dict = parsed
                candidates.append(parsed)

        for candidate in candidates:
            if {"step_number", "status", "summary"}.issubset(candidate):
                return candidate
        return last_dict or (candidates[0] if candidates else None)

    def _git_diff_fallback(
        self,
        working_dir: Path,
        step_number: int,
    ) -> dict[str, Any]:
        """Build a minimal step_result dict from ``git diff --name-status``.

        Sets ``status`` to ``partial`` and ``summary`` to a default message.
        """
        completed = subprocess.run(
            ["git", "diff", "--name-status", "--find-renames", "HEAD"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            raise StepFailure(
                "Failed to compute git diff for Codex fallback",
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        files_changed = []
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            path = parts[-1]
            action = self._map_git_action(status)
            files_changed.append(
                {
                    "path": path,
                    "action": action,
                    "summary": self._default_file_summary(action, path),
                }
            )

        return {
            "step_number": step_number,
            "status": "partial",
            "files_changed": files_changed,
            "summary": "Changes detected via git diff." if files_changed else "No changes detected via git diff.",
            "issues": [],
            "test_commands": [],
        }

    @staticmethod
    def _map_git_action(status: str) -> str:
        prefix = status[:1]
        if prefix == "A":
            return "created"
        if prefix == "D":
            return "deleted"
        return "modified"

    @staticmethod
    def _default_file_summary(action: str, path: str) -> str:
        if action == "created":
            return f"Created {path}"
        if action == "deleted":
            return f"Deleted {path}"
        return f"Modified {path}"

    @staticmethod
    def _is_auth_error(stderr: str) -> bool:
        lowered = stderr.lower()
        return any(pattern in lowered for pattern in _AUTH_PATTERNS)

    def _merge_result_metadata(
        self,
        git_result: dict[str, Any],
        parsed_result: dict[str, Any] | None,
        *,
        step_number: int,
    ) -> dict[str, Any]:
        if not parsed_result:
            return git_result

        ai_changes = {
            change.get("path"): change
            for change in parsed_result.get("files_changed", [])
            if isinstance(change, dict) and change.get("path")
        }
        files_changed = []
        for change in git_result["files_changed"]:
            metadata = ai_changes.get(change["path"], {})
            files_changed.append(
                {
                    "path": change["path"],
                    "action": change["action"],
                    "summary": metadata.get("summary") or change["summary"],
                }
            )

        return {
            "step_number": parsed_result.get("step_number", step_number),
            "status": (
                "partial"
                if parsed_result.get("status") == "success" and not files_changed
                else parsed_result.get("status", git_result["status"])
            ),
            "files_changed": files_changed,
            "summary": parsed_result.get("summary", git_result["summary"]),
            "issues": parsed_result.get("issues", []),
            "test_commands": parsed_result.get("test_commands", []),
            "workspace_diffs": parsed_result.get("workspace_diffs", {}),
        }
