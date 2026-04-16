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
    watchdog_timeout: int = 3600


@dataclass
class ClaudeRoutingConfig:
    model: str = ""
    reasoning_effort: str = "high"


@dataclass
class CodexRoutingConfig:
    model: str = ""
    reasoning_effort: str = "medium"


@dataclass
class PhaseRoutingOverride:
    cli: str = ""
    reasoning_effort: str = ""
    model: str = ""
    model_simple: str = ""
    model_moderate: str = ""
    model_complex: str = ""
    model_architectural: str = ""


@dataclass
class RoutingConfig:
    planner: str = "claude"
    worker: str = "codex"
    reviewer: str = "claude"
    adjudicator: str = "codex"
    feasibility_checker: str = "codex"
    scoper: str = "claude"
    claude: ClaudeRoutingConfig = field(default_factory=ClaudeRoutingConfig)
    codex: CodexRoutingConfig = field(default_factory=CodexRoutingConfig)
    phases: dict[str, PhaseRoutingOverride] = field(default_factory=dict)


@dataclass
class ApprovalConfig:
    require_plan_approval: bool = True
    require_merge_approval: bool = True


@dataclass
class ScopingConfig:
    enabled: bool = True
    # Fixed debate structure uses up to three visible debate rounds.
    max_scoping_rounds: int = 3


@dataclass
class FeasibilityConfig:
    enabled: bool = True
    max_feasibility_replans: int = 2


@dataclass
class DebateConfig:
    escalated_claude_model: str = "claude-opus-4-5-20250514"
    escalated_claude_effort: str = "max"
    escalated_codex_effort: str = "xhigh"


@dataclass
class SessionConfig:
    enable_planning_resume: bool = True
    enable_review_resume: bool = True


def _default_complexity_phase_map(
    *,
    planning: str,
    feasibility: str,
    executing: str,
    reviewing: str,
    adjudicating: str,
) -> dict[str, str]:
    return {
        "planning": planning,
        "feasibility": feasibility,
        "executing": executing,
        "reviewing": reviewing,
        "adjudicating": adjudicating,
    }


@dataclass
class ComplexityRoutingConfig:
    simple: dict[str, str] = field(
        default_factory=lambda: _default_complexity_phase_map(
            planning="medium",
            feasibility="medium",
            executing="medium",
            reviewing="high",
            adjudicating="medium",
        )
    )
    moderate: dict[str, str] = field(
        default_factory=lambda: _default_complexity_phase_map(
            planning="high",
            feasibility="high",
            executing="high",
            reviewing="high",
            adjudicating="medium",
        )
    )
    complex: dict[str, str] = field(
        default_factory=lambda: _default_complexity_phase_map(
            planning="high",
            feasibility="xhigh",
            executing="xhigh",
            reviewing="high",
            adjudicating="high",
        )
    )
    architectural: dict[str, str] = field(
        default_factory=lambda: _default_complexity_phase_map(
            planning="max",
            feasibility="xhigh",
            executing="xhigh",
            reviewing="high",
            adjudicating="high",
        )
    )


@dataclass
class WorktreeConfig:
    base_branch: str = "main"
    branch_prefix: str = "aio/"


@dataclass
class WorkspaceConfig:
    repos: list[str] = field(default_factory=list)


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
    scoping: ScopingConfig = field(default_factory=ScopingConfig)
    feasibility: FeasibilityConfig = field(default_factory=FeasibilityConfig)
    debate: DebateConfig = field(default_factory=DebateConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    complexity_routing: ComplexityRoutingConfig = field(default_factory=ComplexityRoutingConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
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


def _warn_and_strip_deprecated_keys(data: dict[str, Any]) -> None:
    deprecated_keys: list[str] = []

    orchestrator = data.get("orchestrator")
    if isinstance(orchestrator, dict):
        for key in (
            "step_timeout",
            "scoping_timeout",
            "planning_timeout",
            "execution_timeout_low",
            "execution_timeout_medium",
            "execution_timeout_high",
            "review_timeout",
            "adjudication_timeout",
            "max_rework_loops",
            "max_replan_loops",
        ):
            if key in orchestrator:
                deprecated_keys.append(f"orchestrator.{key}")
                orchestrator.pop(key, None)

    feasibility = data.get("feasibility")
    if isinstance(feasibility, dict) and "timeout" in feasibility:
        deprecated_keys.append("feasibility.timeout")
        feasibility.pop("timeout", None)

    routing = data.get("routing")
    if isinstance(routing, dict):
        phases = routing.get("phases")
        if isinstance(phases, dict):
            for phase_name, phase_data in phases.items():
                if isinstance(phase_data, dict) and "max_turns" in phase_data:
                    deprecated_keys.append(f"routing.phases.{phase_name}.max_turns")
                    phase_data.pop("max_turns", None)

    if deprecated_keys:
        warnings.warn(
            "Deprecated config keys are ignored: "
            + ", ".join(deprecated_keys)
            + ". Update the file to the current aio.toml schema.",
            DeprecationWarning,
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


def _validate_string_list(name: str, value: Any) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"Config value '{name}' must be an array of strings")


def _validate_config_tree(data: dict[str, Any]) -> None:
    orchestrator = _expect_mapping("orchestrator", data.get("orchestrator"))
    for key in (
        "max_retries",
        "watchdog_timeout",
    ):
        if key in orchestrator:
            _validate_int(f"orchestrator.{key}", orchestrator[key], minimum=1)

    routing = _expect_mapping("routing", data.get("routing"))
    for key in ("planner", "worker", "reviewer", "adjudicator", "feasibility_checker", "scoper"):
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

    phases = _expect_mapping("routing.phases", routing.get("phases"))
    for phase_name, phase_data in phases.items():
        phase_mapping = _expect_mapping(f"routing.phases.{phase_name}", phase_data)
        for key, value in phase_mapping.items():
            if key in {
                "reasoning_effort",
                "model",
                "model_simple",
                "model_moderate",
                "model_complex",
                "model_architectural",
            }:
                _validate_string(f"routing.phases.{phase_name}.{key}", value)
            elif key == "cli":
                _validate_string(f"routing.phases.{phase_name}.cli", value)
                if value:
                    _validate_choice(
                        f"routing.phases.{phase_name}.cli",
                        value,
                        {"claude", "codex"},
                    )

    scoping = _expect_mapping("scoping", data.get("scoping"))
    if "enabled" in scoping:
        _validate_bool("scoping.enabled", scoping["enabled"])
    if "max_scoping_rounds" in scoping:
        _validate_int("scoping.max_scoping_rounds", scoping["max_scoping_rounds"], minimum=1)

    feasibility = _expect_mapping("feasibility", data.get("feasibility"))
    if "enabled" in feasibility:
        _validate_bool("feasibility.enabled", feasibility["enabled"])
    if "max_feasibility_replans" in feasibility:
        _validate_int(
            "feasibility.max_feasibility_replans",
            feasibility["max_feasibility_replans"],
            minimum=0,
        )

    debate = _expect_mapping("debate", data.get("debate"))
    for key in ("escalated_claude_model", "escalated_claude_effort", "escalated_codex_effort"):
        if key in debate:
            _validate_string(f"debate.{key}", debate[key])

    sessions = _expect_mapping("sessions", data.get("sessions"))
    for key in ("enable_planning_resume", "enable_review_resume"):
        if key in sessions:
            _validate_bool(f"sessions.{key}", sessions[key])

    complexity_routing = _expect_mapping("complexity_routing", data.get("complexity_routing"))
    for tier_name, tier_data in complexity_routing.items():
        tier_mapping = _expect_mapping(f"complexity_routing.{tier_name}", tier_data)
        for phase_name, effort in tier_mapping.items():
            _validate_string(f"complexity_routing.{tier_name}.{phase_name}", effort)

    approval = _expect_mapping("approval", data.get("approval"))
    for key in ("require_plan_approval", "require_merge_approval"):
        if key in approval:
            _validate_bool(f"approval.{key}", approval[key])

    worktree = _expect_mapping("worktree", data.get("worktree"))
    for key in ("base_branch", "branch_prefix"):
        if key in worktree:
            _validate_string(f"worktree.{key}", worktree[key])

    workspace = _expect_mapping("workspace", data.get("workspace"))
    if "repos" in workspace:
        _validate_string_list("workspace.repos", workspace["repos"])

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

    _warn_and_strip_deprecated_keys(merged)
    _validate_config_tree(merged)

    routing_data = _expect_mapping("routing", merged.get("routing"))
    _warn_unknown_keys(
        "root",
        merged,
        {
            "orchestrator",
            "routing",
            "scoping",
            "feasibility",
            "debate",
            "sessions",
            "complexity_routing",
            "approval",
            "worktree",
            "workspace",
            "logging",
            "cli_compat",
        },
    )
    _warn_unknown_keys(
        "routing",
        routing_data,
        {
            "planner",
            "worker",
            "reviewer",
            "adjudicator",
            "feasibility_checker",
            "scoper",
            "claude",
            "codex",
            "phases",
        },
    )
    routing_phases = _expect_mapping("routing.phases", routing_data.get("phases"))
    _warn_unknown_keys(
        "routing.phases",
        routing_phases,
        {"scoping", "planning", "feasibility", "executing", "reviewing", "adjudicating"},
    )
    phase_overrides: dict[str, PhaseRoutingOverride] = {}
    for phase_name, phase_data in routing_phases.items():
        phase_mapping = _expect_mapping(f"routing.phases.{phase_name}", phase_data)
        _warn_unknown_keys(
            f"routing.phases.{phase_name}",
            phase_mapping,
            {
                "cli",
                "reasoning_effort",
                "model",
                "model_simple",
                "model_moderate",
                "model_complex",
                "model_architectural",
            },
        )
        phase_overrides[phase_name] = _apply_section(
            PhaseRoutingOverride,
            phase_mapping,
            section_name=f"routing.phases.{phase_name}",
        )

    complexity_routing_data = _expect_mapping("complexity_routing", merged.get("complexity_routing"))
    _warn_unknown_keys(
        "complexity_routing",
        complexity_routing_data,
        {"simple", "moderate", "complex", "architectural"},
    )
    complexity_routing = _apply_section(
        ComplexityRoutingConfig,
        complexity_routing_data,
        section_name="complexity_routing",
    )
    for tier_name in ("simple", "moderate", "complex", "architectural"):
        _warn_unknown_keys(
            f"complexity_routing.{tier_name}",
            getattr(complexity_routing, tier_name),
            {"planning", "feasibility", "executing", "reviewing", "adjudicating"},
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
            adjudicator=routing_data.get("adjudicator", "codex"),
            feasibility_checker=routing_data.get("feasibility_checker", "codex"),
            scoper=routing_data.get("scoper", "claude"),
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
            phases=phase_overrides,
        ),
        scoping=_apply_section(
            ScopingConfig,
            _expect_mapping("scoping", merged.get("scoping")),
            section_name="scoping",
        ),
        feasibility=_apply_section(
            FeasibilityConfig,
            _expect_mapping("feasibility", merged.get("feasibility")),
            section_name="feasibility",
        ),
        debate=_apply_section(
            DebateConfig,
            _expect_mapping("debate", merged.get("debate")),
            section_name="debate",
        ),
        sessions=_apply_section(
            SessionConfig,
            _expect_mapping("sessions", merged.get("sessions")),
            section_name="sessions",
        ),
        complexity_routing=complexity_routing,
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
        workspace=_apply_section(
            WorkspaceConfig,
            _expect_mapping("workspace", merged.get("workspace")),
            section_name="workspace",
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
