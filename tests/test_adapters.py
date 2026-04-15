from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ai_orchestrator.adapters.base import BlockedOnCLI, InvokeResult, StepFailure
from ai_orchestrator.adapters.claude import ClaudeAdapter
from ai_orchestrator.adapters.codex import CodexAdapter


def _schema(name: str) -> dict:
    return json.loads((Path(__file__).resolve().parents[1] / "schemas" / name).read_text())


class FakePopen:
    def __init__(
        self,
        cmd,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeouts: int = 0,
    ):
        self.cmd = cmd
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._timeouts = timeouts
        self.terminate_called = False
        self.kill_called = False

    def communicate(self, timeout=None):
        if self._timeouts:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired(
                self.cmd,
                timeout,
                output=self._stdout,
                stderr=self._stderr,
            )
        return self._stdout, self._stderr

    def terminate(self):
        self.terminate_called = True
        if self.returncode == 0:
            self.returncode = -15

    def kill(self):
        self.kill_called = True


def _fake_popen_factory(responses, commands, *, matcher=None, fallback_popen=None):
    created = []

    def fake_popen(cmd, **kwargs):
        if matcher is not None and not matcher(cmd):
            assert fallback_popen is not None
            return fallback_popen(cmd, **kwargs)
        commands.append(cmd)
        process = FakePopen(cmd, **responses.pop(0))
        created.append(process)
        return process

    return fake_popen, created


class TestClaudeAdapter:
    def test_invoke_parses_json_and_records_metadata(
        self,
        tmp_path,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        calls = []
        default_config.routing.claude.model = "claude-sonnet"
        default_config.routing.claude.reasoning_effort = "high"
        schema = _schema("plan.schema.json")
        fake_popen, _ = _fake_popen_factory(
            [
                {
                    "stdout": json.dumps(
                        {
                            "plan_id": "00000000-0000-0000-0000-000000000000",
                            "task": "Example",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "description": "One",
                                    "files_to_read": [],
                                    "files_to_modify": ["src/file.py"],
                                    "depends_on": [],
                                    "estimated_complexity": "low",
                                }
                            ],
                            "reasoning": "Keep it simple",
                        }
                    ),
                }
            ],
            calls,
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = ClaudeAdapter(default_config, artifact_root)
        result = adapter.invoke("plan this", tmp_path, 30, schema)

        assert isinstance(result, InvokeResult)
        assert result.data["task"] == "Example"
        assert calls[0][:5] == ["claude", "--model", "claude-sonnet", "--effort", "high"]
        assert calls[0][-2:] == ["--output-format", "json"]
        conn = sqlite3.connect(artifact_root / "metadata.sqlite3")
        row = conn.execute(
            "SELECT cli_name, model, reasoning_effort, output_source FROM invocations"
        ).fetchone()
        conn.close()
        assert row == ("claude", "claude-sonnet", "high", "stdout-json")

    def test_invoke_retries_without_effort_flag_on_unsupported_option(
        self,
        tmp_path,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        schema = _schema("review.schema.json")
        commands = []
        fake_popen, _ = _fake_popen_factory(
            [
                {
                    "stderr": "unknown option '--effort'",
                    "returncode": 2,
                },
                {
                    "stdout": json.dumps(
                        {
                            "review_id": "00000000-0000-0000-0000-000000000000",
                            "verdict": "approve",
                            "score": 8,
                            "findings": [],
                            "summary": "ok",
                            "blocks_merge": False,
                        }
                    ),
                },
            ],
            commands,
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = ClaudeAdapter(default_config, artifact_root)
        result = adapter.invoke("review this", tmp_path, 30, schema)

        assert result.data["verdict"] == "approve"
        assert any(part == "--effort" for part in commands[0])
        assert all(part != "--effort" for part in commands[1])

    def test_invoke_retries_on_old_reasoning_effort_flag(
        self,
        tmp_path,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        schema = _schema("review.schema.json")
        commands = []
        fake_popen, _ = _fake_popen_factory(
            [
                {
                    "stderr": "unknown option '--reasoning-effort'",
                    "returncode": 2,
                },
                {
                    "stdout": json.dumps(
                        {
                            "review_id": "00000000-0000-0000-0000-000000000000",
                            "verdict": "approve",
                            "score": 8,
                            "findings": [],
                            "summary": "ok",
                            "blocks_merge": False,
                        }
                    ),
                },
            ],
            commands,
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = ClaudeAdapter(default_config, artifact_root)
        result = adapter.invoke("review this", tmp_path, 30, schema)

        assert result.data["verdict"] == "approve"
        assert any(part == "--effort" for part in commands[0])
        assert all(part != "--effort" for part in commands[1])

    def test_invoke_extracts_session_id_and_builds_resume_command(
        self,
        tmp_path,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        schema = _schema("review.schema.json")
        commands = []
        fake_popen, _ = _fake_popen_factory(
            [
                {
                    "stdout": json.dumps(
                        {
                            "type": "result",
                            "session_id": "session-123",
                            "result": json.dumps(
                                {
                                    "review_id": "00000000-0000-0000-0000-000000000000",
                                    "verdict": "approve",
                                    "score": 8,
                                    "findings": [],
                                    "summary": "ok",
                                    "blocks_merge": False,
                                }
                            ),
                        }
                    ),
                }
            ],
            commands,
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = ClaudeAdapter(default_config, artifact_root)
        result = adapter.invoke(
            "review this",
            tmp_path,
            30,
            schema,
            resume_session_id="prior-session",
        )

        assert result.session_id == "session-123"
        assert commands[0][commands[0].index("--resume") + 1] == "prior-session"

    def test_invoke_classifies_auth_error_as_blocked(
        self,
        tmp_path,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        fake_popen, _ = _fake_popen_factory(
            [{"stderr": "Authentication required", "returncode": 1}],
            [],
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = ClaudeAdapter(default_config, artifact_root)
        with pytest.raises(BlockedOnCLI):
            adapter.invoke("x", tmp_path, 30, {"title": "Generic", "type": "object"})

    def test_lenient_parse_accepts_fenced_json(self):
        parsed = ClaudeAdapter._lenient_parse("```json\n{\"a\": 1}\n```")
        assert parsed == {"a": 1}

    def test_extract_payload_handles_fenced_result(self):
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": (
                "```json\n"
                '{"actionable": true, "normalized_task": "Do something", '
                '"assumptions": [], "complexity_tier": "simple"}\n'
                "```"
            ),
        }

        with pytest.warns(RuntimeWarning, match="envelope result field"):
            extracted, session_id = ClaudeAdapter._extract_payload(envelope)

        assert extracted["normalized_task"] == "Do something"
        assert session_id is None
        assert "type" not in extracted

    def test_extract_payload_handles_fenced_result_with_leading_whitespace(self):
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": (
                "\n\n```json\n"
                '{"actionable": true, "normalized_task": "Do something", '
                '"assumptions": [], "complexity_tier": "simple"}\n'
                "```"
            ),
        }

        with pytest.warns(RuntimeWarning, match="envelope result field"):
            extracted, session_id = ClaudeAdapter._extract_payload(envelope)

        assert extracted["normalized_task"] == "Do something"
        assert session_id is None
        assert "type" not in extracted

    def test_parse_stdout_handles_fenced_result_in_envelope(self):
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": (
                "\n\n```json\n"
                '{'
                '"actionable": true, '
                '"normalized_task": "Do something", '
                '"assumptions": [], '
                '"complexity_tier": "simple"'
                '}\n'
                "```"
            ),
        }
        adapter = ClaudeAdapter.__new__(ClaudeAdapter)

        with pytest.warns(RuntimeWarning, match="envelope result field"):
            parsed, session_id = adapter._parse_stdout(json.dumps(envelope))

        assert parsed["normalized_task"] == "Do something"
        assert session_id is None
        assert "type" not in parsed

    def test_timeout_terminates_process_before_blocking(
        self,
        tmp_path,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        commands = []
        fake_popen, created = _fake_popen_factory(
            [{"timeouts": 1, "stdout": "", "stderr": ""}],
            commands,
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = ClaudeAdapter(default_config, artifact_root)

        with pytest.raises(BlockedOnCLI):
            adapter.invoke("x", tmp_path, 30, {"title": "Generic", "type": "object"})

        assert created[0].terminate_called is True
        assert created[0].kill_called is False


class TestCodexAdapter:
    def test_invoke_prefers_result_file_and_git_diff_ground_truth(
        self,
        tmp_repo,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        schema = _schema("step_result.schema.json")
        result_file = artifact_root / "results" / "pending-step-1.json"
        result_file.write_text(
            json.dumps(
                {
                    "step_number": 1,
                    "status": "success",
                    "files_changed": [
                        {"path": "wrong.txt", "action": "modified", "summary": "Wrong"}
                    ],
                    "summary": "Implemented",
                    "issues": ["none"],
                    "test_commands": ["pytest"],
                }
            )
        )
        (tmp_repo / "README.md").write_text("# changed\n")

        commands = []
        real_popen = subprocess.Popen
        fake_popen, _ = _fake_popen_factory(
            [{}],
            commands,
            matcher=lambda cmd: cmd[:2] == ["codex", "exec"],
            fallback_popen=real_popen,
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = CodexAdapter(default_config, artifact_root)
        result = adapter.invoke(
            "implement\n/Users/example/.ai-orchestrator/results/pending-step-1.json",
            tmp_repo,
            30,
            schema,
            step_number=1,
        )

        assert result.data["status"] == "success"
        assert result.data["files_changed"] == [
            {"path": "README.md", "action": "modified", "summary": "Modified README.md"}
        ]
        assert commands[0][:7] == [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--config",
            'model_reasoning_effort="medium"',
        ]

    def test_invoke_falls_back_to_stdout_jsonl(
        self,
        tmp_repo,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        schema = _schema("step_result.schema.json")
        (tmp_repo / "README.md").write_text("# changed\n")
        real_popen = subprocess.Popen
        fake_popen, _ = _fake_popen_factory(
            [
                {
                    "stdout": '\n'.join(
                        [
                            '{"event":"start"}',
                            json.dumps(
                                {
                                    "step_number": 1,
                                    "status": "partial",
                                    "files_changed": [],
                                    "summary": "From stdout",
                                    "issues": [],
                                    "test_commands": [],
                                }
                            ),
                        ]
                    ),
                }
            ],
            [],
            matcher=lambda cmd: cmd[:2] == ["codex", "exec"],
            fallback_popen=real_popen,
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = CodexAdapter(default_config, artifact_root)
        result = adapter.invoke(
            "implement\n/Users/example/.ai-orchestrator/results/pending-step-1.json",
            tmp_repo,
            30,
            schema,
            step_number=1,
        )

        assert result.data["summary"] == "From stdout"
        conn = sqlite3.connect(artifact_root / "metadata.sqlite3")
        row = conn.execute(
            "SELECT cli_name, output_source, summary FROM invocations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row == ("codex", "stdout", "From stdout")

    def test_invoke_classifies_auth_error_as_blocked(
        self,
        tmp_repo,
        artifact_root,
        default_config,
        monkeypatch,
    ):
        fake_popen, _ = _fake_popen_factory(
            [{"stderr": "Please log in", "returncode": 1}],
            [],
        )

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        adapter = CodexAdapter(default_config, artifact_root)
        with pytest.raises(BlockedOnCLI):
            adapter.invoke(
                "implement\n/Users/example/.ai-orchestrator/results/pending-step-1.json",
                tmp_repo,
                30,
                _schema("step_result.schema.json"),
                step_number=1,
            )

    def test_scan_stdout_for_json_returns_last_object(self):
        stdout = 'noise\n{"first": true}\nmore\n{"second": true}\n'
        assert CodexAdapter._scan_stdout_for_json(stdout) == {"second": True}
