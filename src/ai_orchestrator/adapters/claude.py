"""Claude Code CLI adapter.

Invokes ``claude -p "<prompt>" --output-format json`` as a subprocess per
AGENTS.md.

Output parsing strategy (strict → lenient fallback):
1. ``json.loads(stdout)`` — strict
2. Strip ANSI, strip markdown fences, find JSON boundaries — lenient (warns)
3. Both fail → ``StepFailure``

Reasoning effort: if configured, passed via flag.  If the flag is unsupported,
the adapter retries without it (graceful degradation, DD-17).

Auth/interactive detection: non-zero exit with known auth-error patterns in
stderr → ``BlockedOnCLI``.  Timeout with no output → ``BlockedOnCLI``.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Any

from ..metadata import InvocationRecord
from .base import BaseAdapter, BlockedOnCLI, StepFailure


# Patterns in stderr that suggest auth or interactive prompts.
_AUTH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"authentication required", re.IGNORECASE),
    re.compile(r"login required", re.IGNORECASE),
    re.compile(r"not authenticated", re.IGNORECASE),
    re.compile(r"please log in", re.IGNORECASE),
    re.compile(r"interactive", re.IGNORECASE),
    re.compile(r"auth(?:entication)? expired", re.IGNORECASE),
]

# ANSI escape sequence pattern
_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class ClaudeAdapter(BaseAdapter):
    """Adapter for the ``claude -p`` CLI.

    See AGENTS.md → Claude Code Adapter for the full contract.
    """

    CLI_NAME = "claude"
    _EFFORT_FLAG = "--effort"
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
    ) -> dict[str, Any]:
        """Invoke ``claude -p`` and return a validated output dict.

        Raises
        ------
        StepFailure
            On non-zero exit, invalid JSON, or schema validation failure.
        BlockedOnCLI
            On auth/interactive exit or timeout with no output.
        """
        command, model, reasoning_effort = self._build_command(
            prompt,
            reasoning_effort_override=reasoning_effort_override,
            model_override=model_override,
        )
        return self._invoke_command(
            command,
            working_dir,
            timeout,
            schema,
            model=model,
            reasoning_effort=reasoning_effort,
            allow_effort_retry=bool(reasoning_effort),
        )

    def _build_command(
        self,
        prompt: str,
        *,
        reasoning_effort_override: str | None = None,
        model_override: str | None = None,
    ) -> tuple[list[str], str | None, str | None]:
        command = [self.CLI_NAME]
        model = model_override or getattr(self._config.routing.claude, "model", "") or None
        reasoning_effort = (
            reasoning_effort_override
            or getattr(self._config.routing.claude, "reasoning_effort", "")
            or None
        )
        if model:
            command.extend([self._MODEL_FLAG, model])
        if reasoning_effort:
            command.extend([self._EFFORT_FLAG, reasoning_effort])
        command.extend(["-p", prompt, "--output-format", "json"])
        return command, model, reasoning_effort

    def _invoke_command(
        self,
        command: list[str],
        working_dir: Path,
        timeout: int,
        schema: dict[str, Any],
        *,
        model: str | None,
        reasoning_effort: str | None,
        allow_effort_retry: bool,
    ) -> dict[str, Any]:
        completed = self._run_subprocess(self.CLI_NAME, command, working_dir, timeout)
        if completed.timed_out:
            if not completed.stdout.strip() and not completed.stderr.strip():
                raise BlockedOnCLI(
                    "Claude CLI timed out without producing output",
                    exit_code=None,
                    stderr=completed.stderr,
                )
            raise StepFailure(
                "Claude CLI timed out",
                exit_code=None,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.exit_code or 0
        raw_log_path = completed.raw_log_path
        started_at = completed.started_at
        finished_at = completed.finished_at

        if exit_code != 0:
            if allow_effort_retry and self._is_unsupported_effort_flag(stderr):
                retry_command = list(command)
                flag_index = retry_command.index(self._EFFORT_FLAG)
                del retry_command[flag_index : flag_index + 2]
                return self._invoke_command(
                    retry_command,
                    working_dir,
                    timeout,
                    schema,
                    model=model,
                    reasoning_effort=None,
                    allow_effort_retry=False,
                )
            if self._is_auth_error(stderr):
                raise BlockedOnCLI(
                    "Claude CLI requires interactive action",
                    exit_code=exit_code,
                    stderr=stderr,
                )
            raise StepFailure(
                "Claude CLI exited with a non-zero status",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )

        parsed = self._parse_stdout(stdout)
        validated = self._validate_output(parsed, schema, working_dir)
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
                model=model,
                reasoning_effort=reasoning_effort,
                output_source="stdout-json",
                summary=typed_result.summary if typed_result else None,
                status=typed_result.status.value if typed_result else None,
                issues=typed_result.issues if typed_result else None,
                test_commands=typed_result.test_commands if typed_result else None,
            )
        )
        return validated

    def _parse_stdout(self, stdout: str) -> dict[str, Any]:
        clean_stdout = self._strip_ansi(stdout).strip()
        try:
            return self._extract_payload(json.loads(clean_stdout))
        except json.JSONDecodeError:
            parsed = self._lenient_parse(clean_stdout)
            if parsed is None:
                raise StepFailure(
                    "Claude CLI did not return valid JSON",
                    stdout=stdout,
                )
            warnings.warn("Claude adapter used lenient JSON parsing", RuntimeWarning)
            return parsed

    @staticmethod
    def _extract_payload(data: Any) -> dict[str, Any]:
        if isinstance(data, dict) and "result" in data:
            result = data["result"]
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                try:
                    nested = json.loads(result)
                except json.JSONDecodeError:
                    lenient = ClaudeAdapter._lenient_parse(result)
                    if lenient is not None:
                        warnings.warn(
                            "Claude adapter used lenient JSON parsing on envelope result field",
                            RuntimeWarning,
                        )
                        return lenient
                else:
                    if isinstance(nested, dict):
                        return nested
        if not isinstance(data, dict):
            raise StepFailure("Claude CLI returned JSON that is not an object")
        return data

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Remove ANSI escape codes from *text*."""
        return _ANSI_ESCAPE.sub("", text)

    @staticmethod
    def _lenient_parse(stdout: str) -> dict[str, Any] | None:
        """Try to extract a JSON object from *stdout* with markdown fence stripping.

        Returns the parsed dict or None if no valid JSON found.
        """
        stripped = stdout.strip()
        fence_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
        if fence_match:
            stripped = fence_match.group(1).strip()

        decoder = json.JSONDecoder()
        for start in range(len(stripped)):
            if stripped[start] != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return ClaudeAdapter._extract_payload(parsed)
        return None

    @staticmethod
    def _is_unsupported_effort_flag(stderr: str) -> bool:
        lowered = stderr.lower()
        return any(flag in lowered for flag in ("--effort", "--reasoning-effort")) and any(
            phrase in lowered for phrase in ("unknown option", "unrecognized option", "unsupported")
        )

    @staticmethod
    def _is_auth_error(stderr: str) -> bool:
        """Return True if *stderr* matches known auth-required patterns."""
        return any(p.search(stderr) for p in _AUTH_PATTERNS)
