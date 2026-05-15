"""Configuration loader for ai-orchestrator.

Reads per-project config from the centralized project directory and merges
it with global defaults.

Uses tomllib (stdlib in Python 3.11+) — no external TOML dependency.

Usage::

    cfg = load_config()
    cfg.orchestrator.max_retries       # int
    cfg.routing.planner                # "claude" | "codex"
    cfg.approval.require_plan_approval # bool
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import warnings

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility fallback for local tests
    import tomli as tomllib


# ---------------------------------------------------------------------------
# Config section dataclasses (mirrors config.toml structure)
# ---------------------------------------------------------------------------

from .paths import get_project_config_path, get_user_data_dir


@dataclass
class OrchestratorConfig:
    max_retries: int = 3
    watchdog_timeout: int = 3600


@dataclass
class PhaseRoutingOverride:
    cli: str = ""
    reasoning_effort: str = ""
    model: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    timeout_seconds: int = 0
    model_simple: str = ""
    model_moderate: str = ""
    model_complex: str = ""
    model_architectural: str = ""
    model_extramax: str = ""


@dataclass
class RoutingConfig:
    planner: str = "claude"
    worker: str = "codex"
    reviewer: str = "claude"
    scoper: str = "claude"
    phases: dict[str, PhaseRoutingOverride] = field(default_factory=dict)


@dataclass
class ApprovalConfig:
    require_plan_approval: bool = True
    require_merge_approval: bool = True


@dataclass
class DeliveryConfig:
    auto_commit: bool = False
    auto_push: bool = False
    commit_message_from_ai: bool = True


@dataclass
class ScopingConfig:
    enabled: bool = True
    participants: list[str] = field(default_factory=lambda: ["claude", "codex"])
    max_rounds: int = 6
    designated_decider: str = ""
    require_user_approval: bool = True


@dataclass
class DefaultModelConfig:
    default: str = ""


@dataclass
class ScopingModelsConfig:
    claude: str = "claude-sonnet-4-6"
    gemini: str = "gemini-2.5-pro"
    codex_light: str = "gpt-5.4-mini"
    codex: str = "gpt-5.4"


@dataclass
class TierModelsConfig:
    simple: str = ""
    moderate: str = ""
    complex: str = ""
    architectural: str = ""
    extramax: str = ""


@dataclass
class ReviewingModelsConfig:
    codex: str = "gpt-5.4"
    gemini: str = "gemini-2.5-pro"


@dataclass
class DebateModelsConfig:
    escalated_claude: str = "claude-opus-4-6"


@dataclass
class ModelsConfig:
    claude: DefaultModelConfig = field(default_factory=DefaultModelConfig)
    codex: DefaultModelConfig = field(default_factory=DefaultModelConfig)
    gemini: DefaultModelConfig = field(default_factory=DefaultModelConfig)
    scoping: ScopingModelsConfig = field(default_factory=ScopingModelsConfig)
    planning: TierModelsConfig = field(default_factory=TierModelsConfig)
    executing: TierModelsConfig = field(default_factory=TierModelsConfig)
    reviewing: ReviewingModelsConfig = field(default_factory=ReviewingModelsConfig)
    debate: DebateModelsConfig = field(default_factory=DebateModelsConfig)


@dataclass
class DefaultEffortConfig:
    default: str = ""


@dataclass
class ScopingEffortsConfig:
    initial: str = "medium"
    comparison: str = "high"
    escalation: str = "xhigh"


@dataclass
class PhaseEffortsConfig:
    planning: str = ""
    executing: str = ""
    reviewing: str = ""


@dataclass
class ComplexityEffortsConfig:
    simple: PhaseEffortsConfig = field(
        default_factory=lambda: PhaseEffortsConfig(
            planning="medium",
            executing="medium",
            reviewing="high",
        )
    )
    moderate: PhaseEffortsConfig = field(
        default_factory=lambda: PhaseEffortsConfig(
            planning="high",
            executing="high",
            reviewing="high",
        )
    )
    complex: PhaseEffortsConfig = field(
        default_factory=lambda: PhaseEffortsConfig(
            planning="high",
            executing="xhigh",
            reviewing="high",
        )
    )
    architectural: PhaseEffortsConfig = field(
        default_factory=lambda: PhaseEffortsConfig(
            planning="xhigh",
            executing="high",
            reviewing="high",
        )
    )
    extramax: PhaseEffortsConfig = field(
        default_factory=lambda: PhaseEffortsConfig(
            planning="max",
            executing="xhigh",
            reviewing="high",
        )
    )


@dataclass
class ReviewFinalEffortsConfig:
    simple: str = "high"
    moderate: str = "high"
    complex: str = "high"
    architectural: str = "xhigh"
    extramax: str = "max"


@dataclass
class ReviewingEffortsConfig:
    codex: str = "high"


@dataclass
class DebateEffortsConfig:
    escalated_claude: str = "xhigh"
    escalated_codex: str = "high"


@dataclass
class EffortsConfig:
    claude: DefaultEffortConfig = field(default_factory=lambda: DefaultEffortConfig(default="high"))
    codex: DefaultEffortConfig = field(default_factory=lambda: DefaultEffortConfig(default="medium"))
    gemini: DefaultEffortConfig = field(default_factory=lambda: DefaultEffortConfig(default="high"))
    scoping: ScopingEffortsConfig = field(default_factory=ScopingEffortsConfig)
    complexity: ComplexityEffortsConfig = field(default_factory=ComplexityEffortsConfig)
    review_final: ReviewFinalEffortsConfig = field(default_factory=ReviewFinalEffortsConfig)
    reviewing: ReviewingEffortsConfig = field(default_factory=ReviewingEffortsConfig)
    debate: DebateEffortsConfig = field(default_factory=DebateEffortsConfig)


@dataclass
class SessionConfig:
    enable_unified_session: bool = True
    enable_planning_resume: bool = True
    enable_review_resume: bool = True


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
class AnalysisModeConfig:
    rounds: int = 3
    claude_model: str = ""
    codex_model: str = ""
    claude_effort: str = "high"
    codex_effort: str = "high"
    escalation_model: str = ""
    escalation_effort: str = "xhigh"


@dataclass
class ReviewModeConfig:
    rounds: int = 3
    claude_model: str = ""
    codex_model: str = ""
    claude_effort: str = "high"
    codex_effort: str = "medium"
    escalation_model: str = ""
    escalation_effort: str = "xhigh"


@dataclass
class AutonomousModeConfig:
    max_iterations: int = 5
    claude_model: str = ""
    codex_model: str = ""
    claude_effort: str = "high"
    codex_effort: str = "medium"


@dataclass
class ModesConfig:
    analysis: AnalysisModeConfig = field(default_factory=AnalysisModeConfig)
    review: ReviewModeConfig = field(default_factory=ReviewModeConfig)
    autonomous: AutonomousModeConfig = field(default_factory=AutonomousModeConfig)


@dataclass
class Config:
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    scoping: ScopingConfig = field(default_factory=ScopingConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    efforts: EffortsConfig = field(default_factory=EffortsConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    cli_compat: CliCompatConfig = field(default_factory=CliCompatConfig)
    modes: ModesConfig = field(default_factory=ModesConfig)


class ConfigError(ValueError):
    """Raised when configuration cannot be parsed or validated."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _global_config_path() -> Path:
    """Return the platform-appropriate global config path."""
    return get_user_data_dir() / "config.toml"


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
    for key in ("planner", "worker", "reviewer", "scoper"):
        if key in routing:
            _validate_choice(f"routing.{key}", routing[key], {"claude", "codex", "gemini"})

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
                "model_extramax",
            }:
                _validate_string(f"routing.phases.{phase_name}.{key}", value)
            elif key == "allowed_tools":
                _validate_string_list(f"routing.phases.{phase_name}.allowed_tools", value)
            elif key == "timeout_seconds":
                _validate_int(f"routing.phases.{phase_name}.timeout_seconds", value, minimum=1)
            elif key == "cli":
                _validate_string(f"routing.phases.{phase_name}.cli", value)
                if value:
                    _validate_choice(
                        f"routing.phases.{phase_name}.cli",
                        value,
                        {"claude", "codex", "gemini"},
                    )

    scoping = _expect_mapping("scoping", data.get("scoping"))
    if "enabled" in scoping:
        _validate_bool("scoping.enabled", scoping["enabled"])
    if "participants" in scoping:
        _validate_string_list("scoping.participants", scoping["participants"])
    if "max_rounds" in scoping:
        _validate_int("scoping.max_rounds", scoping["max_rounds"], minimum=1)
    if "designated_decider" in scoping:
        _validate_string("scoping.designated_decider", scoping["designated_decider"])
    if "require_user_approval" in scoping:
        _validate_bool("scoping.require_user_approval", scoping["require_user_approval"])

    sessions = _expect_mapping("sessions", data.get("sessions"))
    for key in ("enable_unified_session", "enable_planning_resume", "enable_review_resume"):
        if key in sessions:
            _validate_bool(f"sessions.{key}", sessions[key])

    approval = _expect_mapping("approval", data.get("approval"))
    for key in ("require_plan_approval", "require_merge_approval"):
        if key in approval:
            _validate_bool(f"approval.{key}", approval[key])
    delivery = _expect_mapping("delivery", data.get("delivery"))
    for key in ("auto_commit", "auto_push", "commit_message_from_ai"):
        if key in delivery:
            _validate_bool(f"delivery.{key}", delivery[key])

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

    models = _expect_mapping("models", data.get("models"))
    for cli in ("claude", "codex", "gemini"):
        section = _expect_mapping(f"models.{cli}", models.get(cli))
        if "default" in section:
            _validate_string(f"models.{cli}.default", section["default"])
    for phase in ("planning", "executing"):
        section = _expect_mapping(f"models.{phase}", models.get(phase))
        for tier in ("simple", "moderate", "complex", "architectural", "extramax"):
            if tier in section:
                _validate_string(f"models.{phase}.{tier}", section[tier])
    scoping_models = _expect_mapping("models.scoping", models.get("scoping"))
    for key in ("claude", "gemini", "codex_light", "codex"):
        if key in scoping_models:
            _validate_string(f"models.scoping.{key}", scoping_models[key])
    reviewing_models = _expect_mapping("models.reviewing", models.get("reviewing"))
    if "codex" in reviewing_models:
        _validate_string("models.reviewing.codex", reviewing_models["codex"])
    if "gemini" in reviewing_models:
        _validate_string("models.reviewing.gemini", reviewing_models["gemini"])
    debate_models = _expect_mapping("models.debate", models.get("debate"))
    if "escalated_claude" in debate_models:
        _validate_string("models.debate.escalated_claude", debate_models["escalated_claude"])

    efforts = _expect_mapping("efforts", data.get("efforts"))
    for cli in ("claude", "codex", "gemini"):
        section = _expect_mapping(f"efforts.{cli}", efforts.get(cli))
        if "default" in section:
            _validate_string(f"efforts.{cli}.default", section["default"])
    scoping_efforts = _expect_mapping("efforts.scoping", efforts.get("scoping"))
    for key in ("initial", "comparison", "escalation"):
        if key in scoping_efforts:
            _validate_string(f"efforts.scoping.{key}", scoping_efforts[key])
    complexity = _expect_mapping("efforts.complexity", efforts.get("complexity"))
    for tier in ("simple", "moderate", "complex", "architectural", "extramax"):
        tier_section = _expect_mapping(f"efforts.complexity.{tier}", complexity.get(tier))
        for phase in ("planning", "executing", "reviewing"):
            if phase in tier_section:
                _validate_string(f"efforts.complexity.{tier}.{phase}", tier_section[phase])
    review_final = _expect_mapping("efforts.review_final", efforts.get("review_final"))
    for tier in ("simple", "moderate", "complex", "architectural", "extramax"):
        if tier in review_final:
            _validate_string(f"efforts.review_final.{tier}", review_final[tier])
    reviewing_efforts = _expect_mapping("efforts.reviewing", efforts.get("reviewing"))
    if "codex" in reviewing_efforts:
        _validate_string("efforts.reviewing.codex", reviewing_efforts["codex"])
    debate_efforts = _expect_mapping("efforts.debate", efforts.get("debate"))
    for key in ("escalated_claude", "escalated_codex"):
        if key in debate_efforts:
            _validate_string(f"efforts.debate.{key}", debate_efforts[key])

    modes = _expect_mapping("modes", data.get("modes"))
    analysis = _expect_mapping("modes.analysis", modes.get("analysis"))
    review = _expect_mapping("modes.review", modes.get("review"))
    autonomous = _expect_mapping("modes.autonomous", modes.get("autonomous"))
    if "rounds" in analysis:
        _validate_int("modes.analysis.rounds", analysis["rounds"], minimum=1)
    if "rounds" in review:
        _validate_int("modes.review.rounds", review["rounds"], minimum=1)
    if "max_iterations" in autonomous:
        _validate_int("modes.autonomous.max_iterations", autonomous["max_iterations"], minimum=1)
    for name, section in (("modes.analysis", analysis), ("modes.review", review), ("modes.autonomous", autonomous)):
        for key, value in section.items():
            if key in {"rounds", "max_iterations"}:
                continue
            _validate_string(f"{name}.{key}", value)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(repo_root: Path | None = None) -> Config:
    """Load and merge configuration from global and project-scoped TOML files.

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

    repo_path = get_project_config_path(root)
    if repo_path.exists():
        merged = _merge(merged, _load_toml(repo_path))

    # Backward compatibility: map legacy per-round scoping effort keys.
    scoping_efforts = _expect_mapping(
        "efforts.scoping",
        _expect_mapping("efforts", merged.get("efforts")).get("scoping"),
    )
    if "initial" not in scoping_efforts:
        if scoping_efforts.get("round_1_claude"):
            scoping_efforts["initial"] = scoping_efforts["round_1_claude"]
        elif scoping_efforts.get("round_1_codex"):
            scoping_efforts["initial"] = scoping_efforts["round_1_codex"]
    if "comparison" not in scoping_efforts:
        for key in ("round_3_codex", "round_4_claude", "round_5_codex"):
            if scoping_efforts.get(key):
                scoping_efforts["comparison"] = scoping_efforts[key]
                break
    if "escalation" not in scoping_efforts and scoping_efforts.get("round_6_claude"):
        scoping_efforts["escalation"] = scoping_efforts["round_6_claude"]

    _validate_config_tree(merged)

    routing_data = _expect_mapping("routing", merged.get("routing"))
    _warn_unknown_keys(
        "root",
        merged,
        {
            "orchestrator",
            "routing",
            "scoping",
            "models",
            "efforts",
            "sessions",
            "approval",
            "delivery",
            "worktree",
            "workspace",
            "logging",
            "cli_compat",
            "modes",
        },
    )
    models_data = _expect_mapping("models", merged.get("models"))
    _warn_unknown_keys(
        "models",
        models_data,
        {"claude", "codex", "gemini", "scoping", "planning", "executing", "reviewing", "debate"},
    )
    efforts_data = _expect_mapping("efforts", merged.get("efforts"))
    _warn_unknown_keys(
        "efforts",
        efforts_data,
        {"claude", "codex", "gemini", "scoping", "complexity", "review_final", "reviewing", "debate"},
    )
    _warn_unknown_keys(
        "routing",
        routing_data,
        {
            "planner",
            "worker",
            "reviewer",
            "scoper",
            "phases",
        },
    )
    routing_phases = _expect_mapping("routing.phases", routing_data.get("phases"))
    _warn_unknown_keys(
        "routing.phases",
        routing_phases,
        {"scoping", "planning", "executing", "reviewing"},
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
                "allowed_tools",
                "timeout_seconds",
                "model_simple",
                "model_moderate",
                "model_complex",
                "model_architectural",
                "model_extramax",
            },
        )
        phase_overrides[phase_name] = _apply_section(
            PhaseRoutingOverride,
            phase_mapping,
            section_name=f"routing.phases.{phase_name}",
        )

    modes_data = _expect_mapping("modes", merged.get("modes"))
    _warn_unknown_keys("modes", modes_data, {"analysis", "review", "autonomous"})

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
            scoper=routing_data.get("scoper", "claude"),
            phases=phase_overrides,
        ),
        scoping=_apply_section(
            ScopingConfig,
            _expect_mapping("scoping", merged.get("scoping")),
            section_name="scoping",
        ),
        models=ModelsConfig(
            claude=_apply_section(
                DefaultModelConfig,
                _expect_mapping("models.claude", models_data.get("claude")),
                section_name="models.claude",
            ),
            codex=_apply_section(
                DefaultModelConfig,
                _expect_mapping("models.codex", models_data.get("codex")),
                section_name="models.codex",
            ),
            gemini=_apply_section(
                DefaultModelConfig,
                _expect_mapping("models.gemini", models_data.get("gemini")),
                section_name="models.gemini",
            ),
            scoping=_apply_section(
                ScopingModelsConfig,
                _expect_mapping("models.scoping", models_data.get("scoping")),
                section_name="models.scoping",
            ),
            planning=_apply_section(
                TierModelsConfig,
                _expect_mapping("models.planning", models_data.get("planning")),
                section_name="models.planning",
            ),
            executing=_apply_section(
                TierModelsConfig,
                _expect_mapping("models.executing", models_data.get("executing")),
                section_name="models.executing",
            ),
            reviewing=_apply_section(
                ReviewingModelsConfig,
                _expect_mapping("models.reviewing", models_data.get("reviewing")),
                section_name="models.reviewing",
            ),
            debate=_apply_section(
                DebateModelsConfig,
                _expect_mapping("models.debate", models_data.get("debate")),
                section_name="models.debate",
            ),
        ),
        efforts=EffortsConfig(
            claude=_apply_section(
                DefaultEffortConfig,
                _expect_mapping("efforts.claude", efforts_data.get("claude")),
                section_name="efforts.claude",
            ),
            codex=_apply_section(
                DefaultEffortConfig,
                _expect_mapping("efforts.codex", efforts_data.get("codex")),
                section_name="efforts.codex",
            ),
            gemini=_apply_section(
                DefaultEffortConfig,
                _expect_mapping("efforts.gemini", efforts_data.get("gemini")),
                section_name="efforts.gemini",
            ),
            scoping=_apply_section(
                ScopingEffortsConfig,
                _expect_mapping("efforts.scoping", efforts_data.get("scoping")),
                section_name="efforts.scoping",
            ),
            complexity=ComplexityEffortsConfig(
                simple=_apply_section(
                    PhaseEffortsConfig,
                    _expect_mapping(
                        "efforts.complexity.simple",
                        _expect_mapping("efforts.complexity", efforts_data.get("complexity")).get("simple"),
                    ),
                    section_name="efforts.complexity.simple",
                ),
                moderate=_apply_section(
                    PhaseEffortsConfig,
                    _expect_mapping(
                        "efforts.complexity.moderate",
                        _expect_mapping("efforts.complexity", efforts_data.get("complexity")).get("moderate"),
                    ),
                    section_name="efforts.complexity.moderate",
                ),
                complex=_apply_section(
                    PhaseEffortsConfig,
                    _expect_mapping(
                        "efforts.complexity.complex",
                        _expect_mapping("efforts.complexity", efforts_data.get("complexity")).get("complex"),
                    ),
                    section_name="efforts.complexity.complex",
                ),
                architectural=_apply_section(
                    PhaseEffortsConfig,
                    _expect_mapping(
                        "efforts.complexity.architectural",
                        _expect_mapping("efforts.complexity", efforts_data.get("complexity")).get("architectural"),
                    ),
                    section_name="efforts.complexity.architectural",
                ),
                extramax=_apply_section(
                    PhaseEffortsConfig,
                    _expect_mapping(
                        "efforts.complexity.extramax",
                        _expect_mapping("efforts.complexity", efforts_data.get("complexity")).get("extramax"),
                    ),
                    section_name="efforts.complexity.extramax",
                ),
            ),
            review_final=_apply_section(
                ReviewFinalEffortsConfig,
                _expect_mapping("efforts.review_final", efforts_data.get("review_final")),
                section_name="efforts.review_final",
            ),
            reviewing=_apply_section(
                ReviewingEffortsConfig,
                _expect_mapping("efforts.reviewing", efforts_data.get("reviewing")),
                section_name="efforts.reviewing",
            ),
            debate=_apply_section(
                DebateEffortsConfig,
                _expect_mapping("efforts.debate", efforts_data.get("debate")),
                section_name="efforts.debate",
            ),
        ),
        sessions=_apply_section(
            SessionConfig,
            _expect_mapping("sessions", merged.get("sessions")),
            section_name="sessions",
        ),
        approval=_apply_section(
            ApprovalConfig,
            _expect_mapping("approval", merged.get("approval")),
            section_name="approval",
        ),
        delivery=_apply_section(
            DeliveryConfig,
            _expect_mapping("delivery", merged.get("delivery")),
            section_name="delivery",
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
        modes=ModesConfig(
            analysis=_apply_section(
                AnalysisModeConfig,
                _expect_mapping("modes.analysis", modes_data.get("analysis")),
                section_name="modes.analysis",
            ),
            review=_apply_section(
                ReviewModeConfig,
                _expect_mapping("modes.review", modes_data.get("review")),
                section_name="modes.review",
            ),
            autonomous=_apply_section(
                AutonomousModeConfig,
                _expect_mapping("modes.autonomous", modes_data.get("autonomous")),
                section_name="modes.autonomous",
            ),
        ),
    )
    return config
