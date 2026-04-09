"""Configuration loader for ai-orchestrator.

Reads aio.toml at the repo root (current working directory) and merges with
global defaults from ~/.config/ai-orchestrator/config.toml on macOS/Linux or
%APPDATA%\\ai-orchestrator\\config.toml on Windows.

Uses tomllib (stdlib in Python 3.11+) — no external TOML dependency.

Usage::

    cfg = load_config()
    cfg.orchestrator.max_retries       # int
    cfg.routing.planner                # "claude" | "codex"
    cfg.approval.require_plan_approval # bool
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import warnings

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility fallback for local tests
    import tomli as tomllib


# ---------------------------------------------------------------------------
# Config section dataclasses (mirrors aio.toml structure)
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    max_retries: int = 3
    max_rework_loops: int = 3
    max_replan_loops: int = 2
    step_timeout: int = 300
    planning_timeout: int = 120
    execution_timeout_low: int = 180
    execution_timeout_medium: int = 300
    execution_timeout_high: int = 600
    review_timeout: int = 180
    adjudication_timeout: int = 120


@dataclass
class ClaudeRoutingConfig:
    model: str = ""
    reasoning_effort: str = "high"


@dataclass
class CodexRoutingConfig:
    model: str = ""
    reasoning_effort: str = "medium"


@dataclass
class RoutingConfig:
    planner: str = "claude"
    worker: str = "codex"
    reviewer: str = "claude"
    adjudicator: str = "claude"
    claude: ClaudeRoutingConfig = field(default_factory=ClaudeRoutingConfig)
    codex: CodexRoutingConfig = field(default_factory=CodexRoutingConfig)


@dataclass
class ApprovalConfig:
    require_plan_approval: bool = True
    require_merge_approval: bool = True


@dataclass
class WorktreeConfig:
    base_branch: str = "main"
    branch_prefix: str = "aio/"


@dataclass
class LoggingConfig:
    retain_raw_output: bool = False
    retain_prompts: bool = False


@dataclass
class CliCompatConfig:
    claude_min_version: str = ""
    codex_min_version: str = ""


@dataclass
class Config:
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    cli_compat: CliCompatConfig = field(default_factory=CliCompatConfig)


class ConfigError(ValueError):
    """Raised when configuration cannot be parsed or validated."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _global_config_path() -> Path:
    """Return the platform-appropriate global config path."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "ai-orchestrator" / "config.toml"
    return Path.home() / ".config" / "ai-orchestrator" / "config.toml"


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _warn_unknown_keys(section_name: str, data: dict[str, Any], known: set[str]) -> None:
    unknown = sorted(set(data) - known)
    if unknown:
        warnings.warn(
            f"Unknown config keys in '{section_name}': {', '.join(unknown)}",
            RuntimeWarning,
            stacklevel=3,
        )


def _apply_section(section_cls: type, data: dict[str, Any], *, section_name: str) -> Any:
    """Instantiate a dataclass from a dict, warning on unknown keys."""
    known = {f for f in section_cls.__dataclass_fields__}
    _warn_unknown_keys(section_name, data, known)
    filtered = {k: v for k, v in data.items() if k in known}
    return section_cls(**filtered)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Expected TOML table at top level in {path}")
    return data


def _expect_mapping(name: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"Config section '{name}' must be a table")
    return value


def _validate_int(name: str, value: Any, *, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"Config value '{name}' must be an integer")
    if value < minimum:
        raise ConfigError(f"Config value '{name}' must be >= {minimum}")


def _validate_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ConfigError(f"Config value '{name}' must be a boolean")


def _validate_choice(name: str, value: Any, choices: set[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        valid = ", ".join(sorted(choices))
        raise ConfigError(f"Config value '{name}' must be one of: {valid}")


def _validate_string(name: str, value: Any) -> None:
    if not isinstance(value, str):
        raise ConfigError(f"Config value '{name}' must be a string")


def _validate_config_tree(data: dict[str, Any]) -> None:
    orchestrator = _expect_mapping("orchestrator", data.get("orchestrator"))
    for key in (
        "max_retries",
        "max_rework_loops",
        "max_replan_loops",
        "step_timeout",
        "planning_timeout",
        "execution_timeout_low",
        "execution_timeout_medium",
        "execution_timeout_high",
        "review_timeout",
        "adjudication_timeout",
    ):
        if key in orchestrator:
            _validate_int(f"orchestrator.{key}", orchestrator[key], minimum=1)

    routing = _expect_mapping("routing", data.get("routing"))
    for key in ("planner", "worker", "reviewer", "adjudicator"):
        if key in routing:
            _validate_choice(f"routing.{key}", routing[key], {"claude", "codex"})

    claude = _expect_mapping("routing.claude", routing.get("claude"))
    for key in ("model", "reasoning_effort"):
        if key in claude:
            _validate_string(f"routing.claude.{key}", claude[key])

    codex = _expect_mapping("routing.codex", routing.get("codex"))
    for key in ("model", "reasoning_effort"):
        if key in codex:
            _validate_string(f"routing.codex.{key}", codex[key])

    approval = _expect_mapping("approval", data.get("approval"))
    for key in ("require_plan_approval", "require_merge_approval"):
        if key in approval:
            _validate_bool(f"approval.{key}", approval[key])

    worktree = _expect_mapping("worktree", data.get("worktree"))
    for key in ("base_branch", "branch_prefix"):
        if key in worktree:
            _validate_string(f"worktree.{key}", worktree[key])

    logging = _expect_mapping("logging", data.get("logging"))
    for key in ("retain_raw_output", "retain_prompts"):
        if key in logging:
            _validate_bool(f"logging.{key}", logging[key])

    compat = _expect_mapping("cli_compat", data.get("cli_compat"))
    for key in ("claude_min_version", "codex_min_version"):
        if key in compat:
            _validate_string(f"cli_compat.{key}", compat[key])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(repo_root: Path | None = None) -> Config:
    """Load and merge configuration from global and repo-level TOML files.

    Parameters
    ----------
    repo_root:
        Path to the repository root.  Defaults to ``Path.cwd()``.

    Returns
    -------
    Config
        Fully merged configuration object with defaults filled in.

    Raises
    ------
    ValueError
        If a config file exists but contains invalid TOML.
    """
    root = (repo_root or Path.cwd()).resolve()
    merged: dict[str, Any] = asdict(Config())

    global_path = _global_config_path()
    if global_path.exists():
        merged = _merge(merged, _load_toml(global_path))

    repo_path = root / "aio.toml"
    if repo_path.exists():
        merged = _merge(merged, _load_toml(repo_path))

    _validate_config_tree(merged)

    routing_data = _expect_mapping("routing", merged.get("routing"))
    _warn_unknown_keys(
        "root",
        merged,
        {"orchestrator", "routing", "approval", "worktree", "logging", "cli_compat"},
    )
    _warn_unknown_keys(
        "routing",
        routing_data,
        {"planner", "worker", "reviewer", "adjudicator", "claude", "codex"},
    )
    config = Config(
        orchestrator=_apply_section(
            OrchestratorConfig,
            _expect_mapping("orchestrator", merged.get("orchestrator")),
            section_name="orchestrator",
        ),
        routing=RoutingConfig(
            planner=routing_data.get("planner", "claude"),
            worker=routing_data.get("worker", "codex"),
            reviewer=routing_data.get("reviewer", "claude"),
            adjudicator=routing_data.get("adjudicator", "claude"),
            claude=_apply_section(
                ClaudeRoutingConfig,
                _expect_mapping("routing.claude", routing_data.get("claude")),
                section_name="routing.claude",
            ),
            codex=_apply_section(
                CodexRoutingConfig,
                _expect_mapping("routing.codex", routing_data.get("codex")),
                section_name="routing.codex",
            ),
        ),
        approval=_apply_section(
            ApprovalConfig,
            _expect_mapping("approval", merged.get("approval")),
            section_name="approval",
        ),
        worktree=_apply_section(
            WorktreeConfig,
            _expect_mapping("worktree", merged.get("worktree")),
            section_name="worktree",
        ),
        logging=_apply_section(
            LoggingConfig,
            _expect_mapping("logging", merged.get("logging")),
            section_name="logging",
        ),
        cli_compat=_apply_section(
            CliCompatConfig,
            _expect_mapping("cli_compat", merged.get("cli_compat")),
            section_name="cli_compat",
        ),
    )
    return config
