"""Codex CLI adapter.

Invokes ``codex exec --skip-git-repo-check --sandbox workspace-write --json
"<prompt>"`` as a subprocess per AGENTS.md.

Three-tier output strategy:
1. **Result file (primary)**: prompt instructs Codex to write a JSON result to
   ``.ai-orchestrator/results/pending-execution-<run>.json`` or the legacy
   ``.ai-orchestrator/results/pending-step-<n>.json``.
2. **Stdout fallback**: scan stdout from the end for the last valid JSON object.
3. **Git-diff-only fallback**: reconstruct a minimal execution result from
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
from .base import BaseAdapter, BlockedOnCLI, InvokeResult, StepFailure, TextInvokeResult


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

    @classmethod
    def supports_session_resume(cls) -> bool:
        return True

    @classmethod
    def list_available_models(cls) -> list[str]:
        return [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.2",
        ]

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
        resume_session_id: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> InvokeResult:
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
        elif schema_title == "ExecutionResult" and result_path is None:
            raise StepFailure("Codex prompt is missing the pending execution result path")

        command, model, reasoning_effort = self._build_command(
            reasoning_effort_override=reasoning_effort_override,
            model_override=model_override,
            resume_session_id=resume_session_id,
        )
        completed = self._run_subprocess(
            self.CLI_NAME,
            command,
            working_dir,
            timeout,
            stdin_text=prompt,
        )
        stdout = completed.stdout
        thread_id = self._extract_thread_id(stdout)
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
            result = self._scan_stdout_for_json(stdout, required_keys=set(schema.get("required") or []))

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
        elif schema_title == "ExecutionResult":
            try:
                git_result = self._git_diff_execution_fallback(working_dir)
            except StepFailure:
                if result is None or (working_dir / ".git").exists():
                    raise
                validated = self._validate_output(result, schema, working_dir)
            else:
                if result is None:
                    output_source = "git-diff"
                merged = self._merge_execution_result_metadata(git_result, result)
                validated = self._validate_output(merged, schema, working_dir)
        else:
            if result is None:
                raise StepFailure(
                    "Codex CLI did not produce a parseable JSON result",
                    stdout=stdout,
                    stderr=stderr,
                )
            validated = self._validate_output(result, schema, working_dir)
        typed_result = self._typed_step_result(validated)
        typed_execution = self._typed_execution_result(validated)
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
                summary=(typed_result.summary if typed_result else typed_execution.summary if typed_execution else None),
                status=(
                    typed_result.status.value
                    if typed_result
                    else typed_execution.status.value if typed_execution else None
                ),
                issues=(typed_result.issues if typed_result else typed_execution.issues if typed_execution else None),
                test_commands=(
                    typed_result.test_commands
                    if typed_result
                    else typed_execution.test_commands if typed_execution else None
                ),
            )
        )
        return InvokeResult(data=validated, session_id=thread_id)

    def invoke_text(
        self,
        prompt: str,
        working_dir: Path,
        timeout: int,
        *,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> TextInvokeResult:
        command, model, reasoning_effort = self._build_command(
            reasoning_effort_override=reasoning_effort_override,
            model_override=model_override,
            resume_session_id=resume_session_id,
        )
        completed = self._run_subprocess(
            self.CLI_NAME,
            command,
            working_dir,
            timeout,
            stdin_text=prompt,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        thread_id = self._extract_thread_id(stdout)

        if completed.timed_out:
            self._record_invocation(
                InvocationRecord(
                    cli_name=self.CLI_NAME,
                    command=command,
                    working_dir=str(working_dir),
                    timeout_seconds=timeout,
                    exit_code=None,
                    started_at=completed.started_at,
                    finished_at=completed.finished_at,
                    stdout=stdout,
                    stderr=stderr,
                    raw_log_path=str(completed.raw_log_path) if completed.raw_log_path else None,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    output_source="stdout-text",
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
        self._record_invocation(
            InvocationRecord(
                cli_name=self.CLI_NAME,
                command=command,
                working_dir=str(working_dir),
                timeout_seconds=timeout,
                exit_code=exit_code,
                started_at=completed.started_at,
                finished_at=completed.finished_at,
                stdout=stdout,
                stderr=stderr,
                raw_log_path=str(completed.raw_log_path) if completed.raw_log_path else None,
                model=model,
                reasoning_effort=reasoning_effort,
                output_source="stdout-text",
            )
        )
        if exit_code != 0:
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
        return TextInvokeResult(text=self._extract_text_from_jsonl(stdout) or stdout.strip(), session_id=thread_id)

    def _build_command(
        self,
        *,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
    ) -> tuple[list[str], str | None, str | None]:
        command = [
            self.CLI_NAME,
            "exec",
        ]
        if resume_session_id:
            command.extend(["resume", "--skip-git-repo-check", resume_session_id])
        else:
            command.extend(["--skip-git-repo-check", "--sandbox", "workspace-write"])
        command.append("--json")
        model = model_override or getattr(self._config.models.codex, "default", "") or None
        reasoning_effort = (
            reasoning_effort_override
            or getattr(self._config.efforts.codex, "default", "")
            or None
        )
        if model:
            command.extend([self._MODEL_FLAG, model])
        if reasoning_effort:
            command.extend(["--config", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
        command.append("-")
        return command, model, reasoning_effort

    def _pending_result_path(self, step_number: int) -> Path:
        """Return the expected result file path for *step_number*."""
        return self._artifact_root / "results" / f"pending-step-{step_number}.json"

    @staticmethod
    def _extract_thread_id(stdout: str) -> str | None:
        """Return the first Codex JSONL thread id, if present."""
        for line in stdout.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("type") == "thread.started":
                thread_id = parsed.get("thread_id")
                return str(thread_id) if thread_id else None
        return None

    @staticmethod
    def _extract_text_from_jsonl(stdout: str) -> str | None:
        """Extract agent message text from Codex ``--json`` JSONL output."""
        messages: list[str] = []
        for line in stdout.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict) or parsed.get("type") != "item.completed":
                continue
            text = CodexAdapter._agent_message_text(parsed)
            if text:
                messages.append(text)
        return "\n".join(messages).strip() or None

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
    def _scan_stdout_for_json(
        stdout: str,
        required_keys: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Scan stdout from the end for the last valid JSON object.

        Returns the parsed dict or None if no valid JSON found.
        """
        stripped = stdout.strip()
        if not stripped:
            return None

        jsonl_text = CodexAdapter._extract_text_from_jsonl(stripped)
        if jsonl_text:
            parsed_jsonl = CodexAdapter._scan_stdout_for_json(jsonl_text, required_keys=required_keys)
            if parsed_jsonl is not None:
                return parsed_jsonl

        candidates: list[dict[str, Any]] = []
        saw_jsonl_event = False
        for line in reversed(stripped.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "type" in parsed:
                saw_jsonl_event = True
                continue
            if isinstance(parsed, dict):
                candidates.append(parsed)

        if saw_jsonl_event and not candidates:
            return None

        decoder = json.JSONDecoder()
        last_dict: dict[str, Any] | None = None
        for start in range(len(stripped)):
            if stripped[start] != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "type" not in parsed:
                last_dict = parsed
                candidates.append(parsed)

        if required_keys:
            for candidate in candidates:
                if required_keys.issubset(candidate):
                    return candidate

        for candidate in candidates:
            if {"step_number", "status", "summary"}.issubset(candidate):
                return candidate
        return last_dict or (candidates[0] if candidates else None)

    @staticmethod
    def _agent_message_text(event: dict[str, Any]) -> str | None:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            return None
        for key in ("text", "content", "message"):
            value = item.get(key)
            text = CodexAdapter._stringify_message_content(value)
            if text:
                return text
        return None

    @staticmethod
    def _stringify_message_content(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for entry in value:
                if isinstance(entry, str):
                    parts.append(entry)
                elif isinstance(entry, dict):
                    text = entry.get("text") or entry.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts).strip() or None
        return None

    def _git_diff_fallback(
        self,
        working_dir: Path,
        step_number: int,
    ) -> dict[str, Any]:
        """Build a minimal step_result dict from ``git diff --name-status``.

        Sets ``status`` to ``partial`` and ``summary`` to a default message.
        """
        files_changed = self._git_diff_files(working_dir)

        return {
            "step_number": step_number,
            "status": "partial",
            "files_changed": files_changed,
            "summary": "Changes detected via git diff." if files_changed else "No changes detected via git diff.",
            "issues": [],
            "test_commands": [],
        }

    def _git_diff_execution_fallback(self, working_dir: Path) -> dict[str, Any]:
        """Build a minimal full execution result from ``git diff --name-status``."""
        files_changed = self._git_diff_files(working_dir)
        return {
            "status": "partial",
            "files_changed": files_changed,
            "summary": "Changes detected via git diff." if files_changed else "No changes detected via git diff.",
            "issues": [],
            "test_commands": [],
        }

    def _git_diff_files(self, working_dir: Path) -> list[dict[str, str]]:
        completed = subprocess.run(
            ["git", "diff", "--name-status", "--find-renames", "HEAD"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            env=self._filter_env(),
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

        return files_changed

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

    def _merge_execution_result_metadata(
        self,
        git_result: dict[str, Any],
        parsed_result: dict[str, Any] | None,
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
