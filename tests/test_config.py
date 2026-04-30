"""Tests for config loader (src/ai_orchestrator/config.py).

Phase 2 from build-plan.md: config loading, defaults, overrides, invalid config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_orchestrator.config import ConfigError, load_config


class TestLoadConfig:
    def test_defaults_when_no_toml(self, tmp_path):
        """load_config returns default values when no aio.toml exists."""
        cfg = load_config(repo_root=tmp_path)
        assert cfg.orchestrator.max_retries == 3
        assert cfg.orchestrator.watchdog_timeout == 3600
        assert cfg.routing.planner == "claude"
        assert cfg.routing.scoper == "claude"
        assert cfg.scoping.enabled is True
        assert cfg.approval.require_plan_approval is True
        assert cfg.routing.claude.reasoning_effort == "high"
        assert cfg.routing.codex.reasoning_effort == "medium"
        assert cfg.models.scoping.codex_light == "gpt-5.4-mini"
        assert cfg.efforts.scoping.round_6_claude == "xhigh"
        assert cfg.efforts.complexity.architectural.planning == "xhigh"
        assert cfg.efforts.complexity.architectural.executing == "high"
        assert cfg.efforts.complexity.extramax.planning == "max"
        assert cfg.efforts.complexity.extramax.executing == "xhigh"
        assert cfg.models.debate.escalated_claude == "claude-opus-4-6"
        assert cfg.efforts.debate.escalated_claude == "xhigh"

    def test_repo_toml_overrides_defaults(self, tmp_path):
        """Repo-level aio.toml values override defaults."""
        toml = tmp_path / "aio.toml"
        toml.write_text(
            "[orchestrator]\nmax_retries = 5\n"
            "[routing.codex]\nmodel = \"gpt-5\"\nreasoning_effort = \"high\"\n"
        )
        cfg = load_config(repo_root=tmp_path)
        assert cfg.orchestrator.max_retries == 5
        assert cfg.routing.codex.model == "gpt-5"
        assert cfg.routing.codex.reasoning_effort == "high"
        assert cfg.models.codex.default == "gpt-5"
        assert cfg.efforts.codex.default == "high"

    def test_loads_phase_routing_scoping_and_complexity_sections(self, tmp_path):
        (tmp_path / "aio.toml").write_text(
            "\n".join(
                [
                    "[routing]",
                    "[routing.phases.reviewing]",
                    'reasoning_effort = "max"',
                    'allowed_tools = ["Read", "Grep", "Glob", "Bash"]',
                    "timeout_seconds = 7200",
                    "[routing.phases.executing]",
                    'cli = "claude"',
                    'model = "claude-sonnet"',
                    'model_extramax = "claude-opus"',
                    "[scoping]",
                    "enabled = false",
                    "[efforts.complexity.simple]",
                    'reviewing = "max"',
                    "[efforts.complexity.extramax]",
                    'planning = "max"',
                ]
            )
        )

        cfg = load_config(repo_root=tmp_path)

        assert cfg.routing.phases["reviewing"].reasoning_effort == "max"
        assert cfg.routing.phases["reviewing"].allowed_tools == ["Read", "Grep", "Glob", "Bash"]
        assert cfg.routing.phases["reviewing"].timeout_seconds == 7200
        assert cfg.routing.phases["executing"].cli == "claude"
        assert cfg.routing.phases["executing"].model == "claude-sonnet"
        assert cfg.routing.phases["executing"].model_extramax == "claude-opus"
        assert cfg.scoping.enabled is False
        assert cfg.efforts.complexity.simple.reviewing == "max"
        assert cfg.efforts.complexity.extramax.planning == "max"

    def test_global_toml_is_merged_before_repo_overrides(self, tmp_path, monkeypatch):
        global_root = tmp_path / "global"
        config_dir = global_root / ".config" / "ai-orchestrator"
        config_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            "[logging]\nretain_raw_output = true\n"
            "[routing.claude]\nmodel = \"claude-opus\"\n"
        )
        monkeypatch.setattr(Path, "home", lambda: global_root)
        (tmp_path / "aio.toml").write_text("[routing.claude]\nreasoning_effort = \"medium\"\n")

        cfg = load_config(repo_root=tmp_path)
        assert cfg.logging.retain_raw_output is True
        assert cfg.routing.claude.model == "claude-opus"
        assert cfg.routing.claude.reasoning_effort == "medium"

    def test_invalid_toml_raises(self, tmp_path):
        """Invalid TOML raises ValueError."""
        toml = tmp_path / "aio.toml"
        toml.write_text("this is not [ valid toml !!!\n")
        with pytest.raises(ConfigError):
            load_config(repo_root=tmp_path)

    def test_invalid_type_raises(self, tmp_path):
        (tmp_path / "aio.toml").write_text("[orchestrator]\nmax_retries = \"many\"\n")
        with pytest.raises(ConfigError):
            load_config(repo_root=tmp_path)

    def test_unknown_keys_warn(self, tmp_path):
        (tmp_path / "aio.toml").write_text(
            "[orchestrator]\nmax_retries = 5\nunknown_key = 42\n"
            "[routing]\nextra = \"unused\"\n"
        )

        with pytest.warns(RuntimeWarning, match="unknown_key|extra"):
            cfg = load_config(repo_root=tmp_path)

        assert cfg.orchestrator.max_retries == 5
