"""Click CLI entry point for ai-orchestrator."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import click
try:  # pragma: no cover - optional in minimal environments
    import questionary
except Exception:  # pragma: no cover
    questionary = None
try:  # pragma: no cover - optional in minimal test environments
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style as PTStyle
except ImportError:  # pragma: no cover
    PromptSession = None
    FileHistory = None
    KeyBindings = None
    PTStyle = None
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .analysis import AnalysisRunner
from .artifacts import ArtifactError, ArtifactStore
from .bootstrap import (
    ensure_runtime_gitignore,
    install_shell_integration,
    read_install_meta,
    refresh_workflow,
    scaffold_repository,
)
from .config import ARTIFACT_DIR, Config, ConfigError, load_config
from .doctor import run_doctor, run_doctor_fix
from .engine import Engine, EngineError
from .models import RunState
from .modes import AnalysisSettings, AutonomousSettings, Mode, ReviewSettings
from .reviewer.installer import analyze_repo, install_reviewer
from .state import StateError, StateManager
from .ui import (
    ACTIVE_STATES,
    CLAUDE_MODELS,
    CODEX_MODELS,
    EFFORT_LEVELS,
    GEMINI_MODELS,
    MODE_LABELS,
    OrchestratorUI,
    TERMINAL_STATES,
    build_prompt_message,
)
from .worktree import WorktreeManager


_HISTORY_FILE = Path.home() / ".config" / "ai-orchestrator" / "shell_history"


def _build_engine(
    ctx: click.Context,
    *,
    config: Config | None = None,
    skip_review: bool = False,
    autonomous_max_iterations: int | None = None,
    review_rounds: int | None = None,
) -> Engine:
    _ensure_runtime_gitignore(ctx)
    return Engine(
        config or _require_config(ctx),
        ctx.obj["repo_root"],
        ctx.obj["artifact_root"],
        ui=ctx.obj["ui"],
        skip_review=skip_review,
        autonomous_max_iterations=autonomous_max_iterations,
        review_rounds=review_rounds,
    )


def _require_config(ctx: click.Context) -> Config:
    config_error = ctx.obj.get("config_error")
    if config_error:
        raise click.ClickException(config_error)
    return ctx.obj["config"]


def _resolve_workspace_repos(repo_root: Path, config: Config) -> list[str]:
    if (repo_root / ".git").exists():
        return []
    if config.workspace.repos:
        return list(config.workspace.repos)
    return [
        path.name
        for path in sorted(repo_root.iterdir())
        if path.is_dir() and (path / ".git").exists()
    ]


def _resolve_run_id_arg(ctx: click.Context, run_id: str | None) -> str:
    _ensure_runtime_gitignore(ctx)
    state_mgr = StateManager(ctx.obj["artifact_root"])
    try:
        return state_mgr.resolve_run_id(run_id)
    except StateError as exc:
        raise click.ClickException(str(exc)) from exc


def _resolve_session(ctx: click.Context, prefix: str | None) -> tuple[str, str]:
    _ensure_runtime_gitignore(ctx)
    state_mgr = StateManager(ctx.obj["artifact_root"])
    artifacts = ArtifactStore(ctx.obj["artifact_root"])
    normalized = (prefix or "").strip()

    if not normalized or normalized == "latest":
        latest_run_id = state_mgr.resolve_run_id(None) if state_mgr.list_runs() else None
        latest_run_ts = state_mgr.latest_run_timestamp() if latest_run_id else None
        latest_analysis = artifacts.latest_analysis_session()
        latest_analysis_ts = latest_analysis.updated_at if latest_analysis else None
        if latest_run_ts and latest_analysis_ts:
            run_dt = datetime.fromisoformat(latest_run_ts.replace("Z", "+00:00"))
            analysis_dt = datetime.fromisoformat(latest_analysis_ts.replace("Z", "+00:00"))
            if analysis_dt >= run_dt:
                return latest_analysis.session_id, "analysis"
            return latest_run_id, "run"
        if latest_analysis:
            return latest_analysis.session_id, "analysis"
        if latest_run_id:
            return latest_run_id, "run"
        raise click.ClickException("No sessions found.")

    try:
        return state_mgr.resolve_run_id(normalized), "run"
    except StateError:
        pass
    try:
        session = artifacts.load_analysis_session(normalized)
        return session.session_id, "analysis"
    except ArtifactError:
        pass
    except Exception:
        pass
    raise click.ClickException(f"No run or session matches '{normalized}'.")


def _ensure_runtime_gitignore(ctx: click.Context) -> None:
    ensure_runtime_gitignore(ctx.obj["repo_root"])


def _show_home_screen(ctx: click.Context) -> None:
    ui = ctx.obj["ui"]
    repo_name = ctx.obj["repo_root"].name
    has_config = ctx.obj["config_error"] is None
    state_mgr = StateManager(ctx.obj["artifact_root"])
    try:
        run_ids = state_mgr.list_runs()
    except Exception:
        run_ids = []
    states: list[RunState] = []
    for run_id in run_ids:
        try:
            states.append(state_mgr.load(run_id))
        except Exception:
            continue
    active_states = set(ACTIVE_STATES)
    active_runs = [state for state in states if state.status in active_states]
    paused_runs = [state for state in states if state.status == "PAUSED"]
    ui.stderr_console.print(
        ui.render_home_screen(
            repo_name=repo_name,
            version=__version__,
            has_config=has_config,
            active_runs=active_runs,
            paused_runs=paused_runs,
        )
    )
    ctx.obj["home_shown"] = True


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, invoke_without_command=True)
@click.version_option(__version__, prog_name="orch")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Coordinate Claude Code and Codex as workflow agents."""
    ctx.ensure_object(dict)
    repo_root = Path.cwd()
    artifact_root = repo_root / ARTIFACT_DIR
    ui = OrchestratorUI()

    ctx.obj["repo_root"] = repo_root
    ctx.obj["artifact_root"] = artifact_root
    ctx.obj["ui"] = ui
    ctx.obj["config"] = Config()
    ctx.obj["config_error"] = None

    try:
        ctx.obj["config"] = load_config(repo_root=repo_root)
    except ConfigError as exc:
        ctx.obj["config_error"] = str(exc)
    ctx.obj["workspace_repos"] = _resolve_workspace_repos(repo_root, ctx.obj["config"])
    if ctx.invoked_subcommand is None:
        _show_home_screen(ctx)
        _run_shell(ctx)
        ctx.exit()


@main.command("init")
@click.option("--force", is_flag=True, default=False, help="Overwrite default scaffolding files.")
@click.option(
    "--skip-review-setup",
    is_flag=True,
    default=False,
    help="Skip automatic review-install and review-analyze.",
)
@click.pass_context
def cmd_init(ctx: click.Context, force: bool, skip_review_setup: bool) -> None:
    """Scaffold repo config, workflow defaults, and ignore rules."""
    actions = scaffold_repository(ctx.obj["repo_root"], force=force)
    if actions:
        ctx.obj["ui"].print_file_updates(actions)
        if any(action == "created" for action, _ in actions):
            ctx.obj["ui"].print_first_run_tutorial()
    else:
        ctx.obj["ui"].info("Repository already has the default orch scaffolding.")

    if skip_review_setup:
        return

    review_config_path = ctx.obj["repo_root"] / ".ai-review" / "config.json"
    try:
        if force or not review_config_path.exists():
            ctx.invoke(cmd_review_install, force=force)
        ctx.invoke(cmd_review_analyze)
    except Exception as exc:  # pragma: no cover - defensive guard around optional setup
        ctx.obj["ui"].warn(
            f"Review setup skipped (run `orch sync` or the review subcommands manually): {exc}"
        )


def _synthetic_plan(task: str) -> dict[str, object]:
    return {
        "plan_id": str(uuid4()),
        "task": task,
        "approach": "Direct execution requested by the operator.",
        "implementation_steps": [task],
        "key_files": [],
    }


def _load_plan_file(path: str) -> dict[str, object]:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise click.ClickException("Plan file must contain a JSON object.")
    return data


def _load_plan_artifact(store: ArtifactStore, plan_ref: str) -> dict[str, object] | str:
    if plan_ref.endswith(".md"):
        return store.read_text(plan_ref)
    return store.read_json(plan_ref)


def _select_choice(prompt: str, choices: list[str], *, default: str | None = None) -> str | None:
    if questionary is not None and sys.stdin.isatty():
        return questionary.select(prompt, choices=choices, default=default).ask()
    click_choices = click.Choice(choices)
    return click.prompt(prompt, type=click_choices, default=default or choices[0], show_choices=True)


def _confirm_choice(prompt: str, *, default: bool = False) -> bool:
    if questionary is not None and sys.stdin.isatty():
        result = questionary.confirm(prompt, default=default).ask()
        return bool(result)
    return click.confirm(prompt, default=default)


def _available_models_for_cli(engine: Engine, cli_name: str) -> list[str]:
    seen: list[str] = []

    def add(model: str) -> None:
        if model and model not in seen:
            seen.append(model)

    if cli_name == "claude":
        add(engine._config.models.claude.default)
        add(engine._config.models.scoping.claude)
        add(engine._config.models.debate.escalated_claude)
        for tier in ("simple", "moderate", "complex", "architectural", "extramax"):
            add(getattr(engine._config.models.planning, tier))
    elif cli_name == "codex":
        add(engine._config.models.codex.default)
        add(engine._config.models.scoping.codex_light)
        add(engine._config.models.scoping.codex)
        add(engine._config.models.reviewing.codex)
        for tier in ("simple", "moderate", "complex", "architectural", "extramax"):
            add(getattr(engine._config.models.executing, tier))
    elif cli_name == "gemini":
        add(engine._config.models.gemini.default)
        add(engine._config.models.scoping.gemini)
        add(engine._config.models.reviewing.gemini)
    else:
        raise ValueError(f"Unsupported CLI for model listing: {cli_name}")

    phase_override = engine._config.routing.phases.get("executing")
    if phase_override:
        override_cli = phase_override.cli or engine._config.routing.worker
    else:
        override_cli = ""
    if phase_override and override_cli == cli_name:
        add(phase_override.model)
        for tier_field in (
            "model_simple",
            "model_moderate",
            "model_complex",
            "model_architectural",
            "model_extramax",
        ):
            add(getattr(phase_override, tier_field, ""))

    return ["(default)", *seen] if seen else ["(default)"]


def _shell_model_choices(ctx: click.Context, cli_name: str) -> list[str]:
    if cli_name == "claude":
        base = CLAUDE_MODELS
    elif cli_name == "codex":
        base = CODEX_MODELS
    else:
        base = GEMINI_MODELS
    merged = [*base]
    for model in _available_models_for_cli(_build_engine(ctx), cli_name):
        if model not in merged:
            merged.append(model)
    return merged


def _normalize_shell_count(value: int, unlimited_value: int) -> int:
    return unlimited_value if value == 0 else value


def _settings_mode_label(mode: Mode) -> str:
    return MODE_LABELS.get(mode.value, mode.value) or "default"


def _mode_settings_summary(mode_state: dict[str, Any]) -> str:
    mode = mode_state.get("mode", Mode.DEFAULT)
    if mode == Mode.ANALYSIS:
        s = mode_state["analysis"]
        rounds = "unlimited" if s.rounds >= 999 else str(s.rounds)
        return (
            f"analysis · Claude: {s.claude_model or 'default'} ({s.claude_effort}) · "
            f"Codex: {s.codex_model or 'default'} ({s.codex_effort}) · Rounds: {rounds}"
        )
    if mode == Mode.QUICK_EXECUTE:
        s = mode_state["execute"]
        return (
            f"execute · Executor: {s['cli']} · Model: {s['model'] or 'default'} ({s['effort'] or 'default'}) · "
            f"Skip review: {'yes' if s['skip_review'] else 'no'}"
        )
    if mode == Mode.REVIEW:
        s = mode_state["review"]
        rounds = "unlimited" if s.rounds >= 999 else str(s.rounds)
        return (
            f"review · Claude: {s.claude_model or 'default'} ({s.claude_effort}) · "
            f"Codex: {s.codex_model or 'default'} ({s.codex_effort}) · Rounds: {rounds}"
        )
    if mode == Mode.AUTONOMOUS:
        s = mode_state["autonomous"]
        limit = "unlimited" if s.max_iterations >= 999 else str(s.max_iterations)
        return (
            f"auto · Claude: {s.claude_model or 'default'} ({s.claude_effort}) · "
            f"Codex: {s.codex_model or 'default'} ({s.codex_effort}) · Max iterations: {limit}"
        )
    return "default · Settings at approval gates"


def _configure_mode_settings(ctx: click.Context, mode_state: dict[str, Any]) -> None:
    ui = ctx.obj["ui"]
    mode = mode_state.get("mode", Mode.DEFAULT)
    if mode == Mode.DEFAULT:
        config = _require_config(ctx)
        ui.info(
            "Current settings (change at plan approval gate) | "
            f"Claude: {config.models.claude.default or 'default'} ({config.efforts.claude.default}) · "
            f"Codex: {config.models.codex.default or 'default'} ({config.efforts.codex.default})"
        )
        return
    if mode == Mode.ANALYSIS:
        s: AnalysisSettings = mode_state["analysis"]
        rounds = click.prompt("Rounds (0 for unlimited)", type=int, default=(0 if s.rounds >= 999 else s.rounds))
        s.rounds = _normalize_shell_count(rounds, 999)
        claude_model = _select_choice("Claude model", _shell_model_choices(ctx, "claude"), default=s.claude_model or "(default)")
        if claude_model:
            s.claude_model = "" if claude_model == "(default)" else claude_model
        claude_effort = _select_choice("Claude effort", EFFORT_LEVELS, default=s.claude_effort)
        if claude_effort:
            s.claude_effort = claude_effort
        codex_model = _select_choice("Codex model", _shell_model_choices(ctx, "codex"), default=s.codex_model or "(default)")
        if codex_model:
            s.codex_model = "" if codex_model == "(default)" else codex_model
        codex_effort = _select_choice("Codex effort", EFFORT_LEVELS, default=s.codex_effort)
        if codex_effort:
            s.codex_effort = codex_effort
        if s.rounds < 999:
            esc_model = _select_choice(
                "Escalation model (final round)",
                _shell_model_choices(ctx, "claude"),
                default=s.escalation_model or "(default)",
            )
            if esc_model:
                s.escalation_model = "" if esc_model == "(default)" else esc_model
            esc_effort = _select_choice("Escalation effort", EFFORT_LEVELS, default=s.escalation_effort)
            if esc_effort:
                s.escalation_effort = esc_effort
        else:
            s.escalation_model = ""
            s.escalation_effort = ""
        ui.info(_mode_settings_summary(mode_state))
        return
    if mode == Mode.QUICK_EXECUTE:
        s: dict[str, Any] = mode_state["execute"]
        cli_name = _select_choice("Executor", ["codex", "claude"], default=str(s["cli"]))
        if cli_name:
            s["cli"] = cli_name
        model = _select_choice("Model", _shell_model_choices(ctx, s["cli"]), default=s["model"] or "(default)")
        if model:
            s["model"] = "" if model == "(default)" else model
        effort = _select_choice("Effort", EFFORT_LEVELS, default=(s["effort"] or "high"))
        if effort:
            s["effort"] = effort
        s["skip_review"] = _confirm_choice("Skip review?", default=bool(s["skip_review"]))
        ui.info(_mode_settings_summary(mode_state))
        return
    if mode == Mode.REVIEW:
        s: ReviewSettings = mode_state["review"]
        rounds = click.prompt("Rounds (0 for unlimited)", type=int, default=(0 if s.rounds >= 999 else s.rounds))
        s.rounds = _normalize_shell_count(rounds, 999)
        claude_model = _select_choice("Claude model", _shell_model_choices(ctx, "claude"), default=s.claude_model or "(default)")
        if claude_model:
            s.claude_model = "" if claude_model == "(default)" else claude_model
        claude_effort = _select_choice("Claude effort", EFFORT_LEVELS, default=s.claude_effort)
        if claude_effort:
            s.claude_effort = claude_effort
        codex_model = _select_choice("Codex model", _shell_model_choices(ctx, "codex"), default=s.codex_model or "(default)")
        if codex_model:
            s.codex_model = "" if codex_model == "(default)" else codex_model
        codex_effort = _select_choice("Codex effort", EFFORT_LEVELS, default=s.codex_effort)
        if codex_effort:
            s.codex_effort = codex_effort
        if s.rounds < 999:
            esc_model = _select_choice("Escalation model", _shell_model_choices(ctx, "claude"), default=s.escalation_model or "(default)")
            if esc_model:
                s.escalation_model = "" if esc_model == "(default)" else esc_model
            esc_effort = _select_choice("Escalation effort", EFFORT_LEVELS, default=s.escalation_effort)
            if esc_effort:
                s.escalation_effort = esc_effort
        else:
            s.escalation_model = ""
            s.escalation_effort = ""
        ui.info(_mode_settings_summary(mode_state))
        return
    if mode == Mode.AUTONOMOUS:
        s: AutonomousSettings = mode_state["autonomous"]
        limit = click.prompt("Max fix iterations (0 for unlimited)", type=int, default=(0 if s.max_iterations >= 999 else s.max_iterations))
        s.max_iterations = _normalize_shell_count(limit, 999)
        claude_model = _select_choice("Claude model", _shell_model_choices(ctx, "claude"), default=s.claude_model or "(default)")
        if claude_model:
            s.claude_model = "" if claude_model == "(default)" else claude_model
        claude_effort = _select_choice("Claude effort", EFFORT_LEVELS, default=s.claude_effort)
        if claude_effort:
            s.claude_effort = claude_effort
        codex_model = _select_choice("Codex model", _shell_model_choices(ctx, "codex"), default=s.codex_model or "(default)")
        if codex_model:
            s.codex_model = "" if codex_model == "(default)" else codex_model
        codex_effort = _select_choice("Codex effort", EFFORT_LEVELS, default=s.codex_effort)
        if codex_effort:
            s.codex_effort = codex_effort
        ui.info(_mode_settings_summary(mode_state))


def _adjust_execution_settings(
    ui: OrchestratorUI,
    engine: Engine,
    state_mgr: StateManager,
    state: RunState,
    run_id: str,
) -> None:
    ui.stderr_console.print(
        Panel(
            "The execution model and reasoning level are tuned for this task's complexity tier. "
            "Overrides may increase token usage without improving quality, or reduce quality if "
            "a weaker configuration is selected.",
            title="Override Warning",
            border_style="yellow",
        )
    )

    what = _select_choice(
        "What would you like to adjust?",
        ["Model", "Reasoning level", "Both", "Executor (Claude/Codex/Gemini)", "Cancel"],
        default="Cancel",
    )
    if not what or what == "Cancel":
        return

    overrides = dict(state.execution_overrides or {})
    current_cli = overrides.get("cli") or engine._phase_cli("executing", config_name="worker")

    if what in {"Model", "Both"}:
        selected_model = _select_choice(
            "Select model:",
            _available_models_for_cli(engine, current_cli),
            default="(default)",
        )
        if selected_model:
            if selected_model == "(default)":
                overrides.pop("model", None)
            else:
                overrides["model"] = selected_model

    if what in {"Reasoning level", "Both"}:
        selected_effort = _select_choice(
            "Select reasoning level:",
            ["medium", "high", "xhigh", "max"],
            default=(overrides.get("effort") or "high"),
        )
        if selected_effort:
            overrides["effort"] = selected_effort

    if what == "Executor (Claude/Codex/Gemini)":
        target_cli = _select_choice(
            "Select executor:",
            ["codex", "claude", "gemini"],
            default=current_cli,
        )
        if target_cli and target_cli != current_cli and _confirm_choice(
            f"Switch executor from {current_cli} to {target_cli}?",
            default=False,
        ):
            overrides["cli"] = target_cli
            selected_model = _select_choice(
                f"Select {target_cli} model:",
                _available_models_for_cli(engine, target_cli),
                default="(default)",
            )
            if selected_model:
                if selected_model == "(default)":
                    overrides.pop("model", None)
                else:
                    overrides["model"] = selected_model

    state.execution_overrides = overrides
    state_mgr.save(state)
    ui.info(f"Execution settings updated for run {run_id}.")


def _start_run(
    ctx: click.Context,
    task: str,
    interactive: bool,
    *,
    skip_scoping: bool,
    skip_planning: bool = False,
    start_at: str | None = None,
    plan_file: str | None = None,
    mode: str = Mode.DEFAULT.value,
    skip_review: bool = False,
    config: Config | None = None,
    autonomous_max_iterations: int | None = None,
) -> RunState:
    active_config = config or _require_config(ctx)
    if skip_scoping:
        active_config.scoping.enabled = False
    plan = _load_plan_file(plan_file) if plan_file else None
    if skip_planning and start_at is None:
        start_at = "executing"
    if start_at in {"executing", "execution", "reviewing", "review"} and plan is None:
        plan = _synthetic_plan(task)
    engine = _build_engine(
        ctx,
        config=active_config,
        skip_review=skip_review,
        autonomous_max_iterations=autonomous_max_iterations,
    )
    run_id = str(uuid4())
    workspace_repos = ctx.obj["workspace_repos"]
    state = engine.start(
        task,
        run_id,
        is_workspace=bool(workspace_repos),
        workspace_repos=workspace_repos,
        start_at=start_at,
        plan=plan,
        mode=mode,
    )
    if interactive:
        state = _drive_interactive_approvals(ctx, state.run_id)
    _render_run_snapshot(ctx, state.run_id, state=state)
    return state


@main.command("new", hidden=True)
@click.argument("task", required=False)
@click.option(
    "--interactive/--no-interactive",
    default=True,
    help="Drive approval gates inline instead of returning control at the first pause.",
)
@click.option("--detach", is_flag=True, default=False, help="Alias for --no-interactive.")
@click.option("--skip-scoping", is_flag=True, default=False)
@click.option("--skip-planning", is_flag=True, default=False)
@click.option("--start-at", type=click.Choice(["scoping", "planning", "executing", "reviewing"]))
@click.option("--plan-file", type=click.Path(dir_okay=False, path_type=str))
@click.pass_context
def cmd_new(
    ctx: click.Context,
    task: str | None,
    interactive: bool,
    detach: bool,
    skip_scoping: bool,
    skip_planning: bool,
    start_at: str | None,
    plan_file: str | None,
) -> None:
    """Start a new orchestrated run for TASK."""
    ctx.invoke(
        cmd_run,
        task=task,
        interactive=interactive,
        detach=detach,
        skip_scoping=skip_scoping,
        skip_planning=skip_planning,
        start_at=start_at,
        plan_file=plan_file,
    )


@main.command("run")
@click.argument("task", required=False)
@click.option(
    "--interactive/--no-interactive",
    default=True,
    help="Drive approval gates inline instead of returning control at the first pause.",
)
@click.option("--detach", is_flag=True, default=False, help="Alias for --no-interactive.")
@click.option("--skip-scoping", is_flag=True, default=False)
@click.option("--skip-planning", is_flag=True, default=False)
@click.option("--start-at", type=click.Choice(["scoping", "planning", "executing", "reviewing"]))
@click.option("--plan-file", type=click.Path(dir_okay=False, path_type=str))
@click.pass_context
def cmd_run(
    ctx: click.Context,
    task: str | None,
    interactive: bool,
    detach: bool,
    skip_scoping: bool,
    skip_planning: bool,
    start_at: str | None,
    plan_file: str | None,
) -> None:
    """Start a new orchestrated run for TASK."""
    if task is None:
        task = _read_task_from_stdin_or_prompt()
    interactive = interactive and not detach
    _start_run(
        ctx,
        task,
        interactive,
        skip_scoping=skip_scoping,
        skip_planning=skip_planning,
        start_at=start_at,
        plan_file=plan_file,
    )


@main.command("shell", hidden=True)
@click.pass_context
def cmd_shell(ctx: click.Context) -> None:
    """Open the interactive ai-orchestrator shell."""
    _run_shell(ctx)


@main.command("execute")
@click.argument("task_or_plan", required=False)
@click.option("--no-review", is_flag=True, default=False, help="Skip the review phase after execution.")
@click.option("--interactive/--no-interactive", default=True)
@click.option("--detach", is_flag=True, default=False, help="Alias for --no-interactive.")
@click.pass_context
def cmd_execute(
    ctx: click.Context,
    task_or_plan: str | None,
    no_review: bool,
    interactive: bool,
    detach: bool,
) -> None:
    """Quick execute a task or an existing natural plan JSON file."""
    if task_or_plan is None:
        task_or_plan = _read_task_from_stdin_or_prompt()
    plan_file = task_or_plan if Path(task_or_plan).expanduser().is_file() else None
    task = task_or_plan
    if plan_file:
        plan = _load_plan_file(plan_file)
        task = str(plan.get("task") or "Execute provided plan")
    interactive = interactive and not detach
    _start_run(
        ctx,
        task,
        interactive,
        skip_scoping=True,
        start_at="executing",
        plan_file=plan_file,
        mode=Mode.QUICK_EXECUTE.value,
        skip_review=no_review,
    )


@main.command("review")
@click.argument("task", required=False)
@click.option("--rounds", type=int, default=None, help="Review debate rounds.")
@click.option("--escalation-model", default="", help="Escalation model for review debate.")
@click.option("--escalation-effort", default="", help="Escalation reasoning effort for review debate.")
@click.option("--interactive/--no-interactive", default=True)
@click.option("--detach", is_flag=True, default=False, help="Alias for --no-interactive.")
@click.pass_context
def cmd_review(
    ctx: click.Context,
    task: str | None,
    rounds: int | None,
    escalation_model: str,
    escalation_effort: str,
    interactive: bool,
    detach: bool,
) -> None:
    """Review the current branch diff without running planning or execution."""
    task = task or "Review the current branch diff."
    settings = ReviewSettings(
        rounds=rounds or _require_config(ctx).modes.review.rounds,
        claude_model=_require_config(ctx).modes.review.claude_model or _require_config(ctx).models.claude.default,
        codex_model=_require_config(ctx).modes.review.codex_model or _require_config(ctx).models.codex.default,
        claude_effort=_require_config(ctx).modes.review.claude_effort or _require_config(ctx).efforts.claude.default,
        codex_effort=_require_config(ctx).modes.review.codex_effort or _require_config(ctx).efforts.codex.default,
        escalation_model=escalation_model or _require_config(ctx).modes.review.escalation_model,
        escalation_effort=escalation_effort or _require_config(ctx).modes.review.escalation_effort,
    )
    ctx.obj["ui"].print_mode_header(Mode.REVIEW.value, settings)
    interactive = interactive and not detach
    config = deepcopy(_require_config(ctx))
    if settings.escalation_model:
        config.models.debate.escalated_claude = settings.escalation_model
    if settings.escalation_effort:
        config.efforts.debate.escalated_claude = settings.escalation_effort
    engine = _build_engine(ctx, config=config, review_rounds=settings.rounds)
    run_id = str(uuid4())
    state = engine.start(
        task,
        run_id,
        is_workspace=True,
        workspace_repos=[],
        start_at="reviewing",
        plan=_synthetic_plan(task),
        mode=Mode.REVIEW.value,
    )
    if interactive:
        state = _drive_interactive_approvals(ctx, state.run_id)
    _render_run_snapshot(ctx, state.run_id, state=state)


@main.command("analysis")
@click.argument("task", required=False)
@click.option("--rounds", type=int, default=None)
@click.option("--claude-model", default="")
@click.option("--codex-model", default="")
@click.option("--claude-effort", default="")
@click.option("--codex-effort", default="")
@click.option("--escalation-model", default="")
@click.option("--escalation-effort", default="")
@click.pass_context
def cmd_analysis(
    ctx: click.Context,
    task: str | None,
    rounds: int | None,
    claude_model: str,
    codex_model: str,
    claude_effort: str,
    codex_effort: str,
    escalation_model: str,
    escalation_effort: str,
) -> None:
    """Run parallel AI analysis and debate without changing files."""
    task = task or _read_task_from_stdin_or_prompt()
    config = _require_config(ctx)
    settings = AnalysisSettings(
        rounds=rounds or config.modes.analysis.rounds,
        claude_model=claude_model or config.modes.analysis.claude_model or config.models.claude.default,
        codex_model=codex_model or config.modes.analysis.codex_model or config.models.codex.default,
        claude_effort=claude_effort or config.modes.analysis.claude_effort or config.efforts.claude.default,
        codex_effort=codex_effort or config.modes.analysis.codex_effort or config.efforts.codex.default,
        escalation_model=escalation_model or config.modes.analysis.escalation_model,
        escalation_effort=escalation_effort or config.modes.analysis.escalation_effort,
    )
    ctx.obj["ui"].print_mode_header(Mode.ANALYSIS.value, settings)
    AnalysisRunner(config, ctx.obj["repo_root"], ctx.obj["artifact_root"], ui=ctx.obj["ui"]).run(
        task,
        settings,
    )


@main.command("auto")
@click.argument("task", required=False)
@click.option("--max-iterations", type=int, default=None)
@click.pass_context
def cmd_auto(
    ctx: click.Context,
    task: str | None,
    max_iterations: int | None,
) -> None:
    """Run the full pipeline without approval gates."""
    task = task or _read_task_from_stdin_or_prompt()
    config = deepcopy(_require_config(ctx))
    config.approval.require_plan_approval = False
    config.approval.require_merge_approval = False
    limit = max_iterations or config.modes.autonomous.max_iterations
    ctx.obj["ui"].print_mode_header(Mode.AUTONOMOUS.value, AutonomousSettings(max_iterations=limit))
    state = _start_run(
        ctx,
        task,
        False,
        skip_scoping=False,
        mode=Mode.AUTONOMOUS.value,
        config=config,
        autonomous_max_iterations=limit,
    )
    if state.status == "PAUSED" and state.current_phase == "REVIEWING":
        if _confirm_choice(f"Autonomous fix limit ({limit}) reached. Continue?", default=False):
            state.fix_iteration_count = 0
            StateManager(ctx.obj["artifact_root"]).save(state)
            resumed = _build_engine(
                ctx,
                config=config,
                autonomous_max_iterations=limit,
            ).resume(state.run_id)
            _render_run_snapshot(ctx, resumed.run_id, state=resumed)
        else:
            ctx.obj["ui"].warning("Autonomous run left paused.")


@main.command("sessions")
@click.option("--mode", "mode_filter", default="all", type=click.Choice(["all", "analysis", "review", "auto", "default", "quick_execute", "autonomous"]))
@click.pass_context
def cmd_sessions(ctx: click.Context, mode_filter: str) -> None:
    """List past orchestrator sessions."""
    normalized = "autonomous" if mode_filter == "auto" else mode_filter
    sessions = ArtifactStore(ctx.obj["artifact_root"]).list_sessions(normalized)
    lines = [
        f"{item['session_id'][:8]}  {item['mode']:<13} {item['status']:<12} {item['timestamp']}  {item['task'][:80]}"
        for item in sessions
    ]
    ctx.obj["ui"].print_logs("\n".join(lines) if lines else "No sessions found.", title="Sessions")


@main.command("continue")
@click.argument("session_id", required=False, default=None)
@click.argument("follow_up", required=False)
@click.pass_context
def cmd_continue(ctx: click.Context, session_id: str | None, follow_up: str | None) -> None:
    """Continue the latest or a specific session (run or analysis)."""
    config = _require_config(ctx)
    resolved_id, kind = _resolve_session(ctx, session_id)
    if kind == "run":
        if follow_up:
            ctx.obj["ui"].warning("follow_up is ignored for pipeline runs.")
        engine = _build_engine(ctx)
        try:
            state = engine.resume(resolved_id)
        except EngineError as exc:
            raise click.ClickException(str(exc)) from exc
        _render_run_snapshot(ctx, state.run_id, state=state)
        return
    settings = AnalysisSettings(
        rounds=config.modes.analysis.rounds,
        claude_model=config.modes.analysis.claude_model or config.models.claude.default,
        codex_model=config.modes.analysis.codex_model or config.models.codex.default,
        claude_effort=config.modes.analysis.claude_effort or config.efforts.claude.default,
        codex_effort=config.modes.analysis.codex_effort or config.efforts.codex.default,
        escalation_model=config.modes.analysis.escalation_model,
        escalation_effort=config.modes.analysis.escalation_effort,
    )
    AnalysisRunner(config, ctx.obj["repo_root"], ctx.obj["artifact_root"], ui=ctx.obj["ui"]).continue_session(
        resolved_id,
        follow_up or "",
        settings,
    )


def _read_task_from_stdin_or_prompt() -> str:
    if not sys.stdin.isatty():
        task = sys.stdin.read().strip()
        if task:
            return task
    return click.prompt("Task", type=str)


def _create_prompt_session(mode_state: dict[str, Any] | None = None, ctx: click.Context | None = None):
    if PromptSession is None or FileHistory is None or KeyBindings is None or PTStyle is None:
        raise click.ClickException(
            "prompt_toolkit is required for interactive shell mode. "
            "Install ai-orchestrator with its project dependencies.",
        )
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event):
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    if mode_state is not None:
        modes = list(Mode)

        @bindings.add("s-tab")
        def cycle_mode(event):
            current = mode_state.get("mode", Mode.DEFAULT)
            new_mode = modes[(modes.index(current) + 1) % len(modes)]
            mode_state["mode"] = new_mode
            mode_state["formatted_prompt"] = build_prompt_message(new_mode.value)
            event.app.invalidate()

    def toolbar() -> str:
        if mode_state is None:
            return ""
        return f"shift+tab {_mode_settings_summary(mode_state)} · /config · /help"

    pt_style = PTStyle.from_dict({"bottom-toolbar": "noreverse #888888"})

    return PromptSession(
        history=FileHistory(str(_HISTORY_FILE)),
        key_bindings=bindings,
        multiline=False,
        enable_open_in_editor=True,
        bottom_toolbar=toolbar,
        style=pt_style,
    )


def _run_shell(ctx: click.Context) -> None:
    ui = ctx.obj["ui"]
    repo_name = ctx.obj["repo_root"].name
    console = ui.stderr_console
    config = _require_config(ctx)
    if not ctx.obj.get("home_shown"):
        console.print()
        console.print(f"  [bold cyan]ai-orchestrator[/bold cyan] [dim]{__version__}[/dim]  [dim]·  ~/{repo_name}[/dim]")
        console.print()
    mode_state: dict[str, Any] = {
        "mode": Mode.DEFAULT,
        "formatted_prompt": build_prompt_message(Mode.DEFAULT.value),
        "analysis": AnalysisSettings(
            rounds=config.modes.analysis.rounds,
            claude_model=config.modes.analysis.claude_model or config.models.claude.default,
            codex_model=config.modes.analysis.codex_model or config.models.codex.default,
            claude_effort=config.modes.analysis.claude_effort or config.efforts.claude.default,
            codex_effort=config.modes.analysis.codex_effort or config.efforts.codex.default,
            escalation_model=config.modes.analysis.escalation_model,
            escalation_effort=config.modes.analysis.escalation_effort,
        ),
        "review": ReviewSettings(
            rounds=config.modes.review.rounds,
            claude_model=config.modes.review.claude_model or config.models.claude.default,
            codex_model=config.modes.review.codex_model or config.models.codex.default,
            claude_effort=config.modes.review.claude_effort or config.efforts.claude.default,
            codex_effort=config.modes.review.codex_effort or config.efforts.codex.default,
            escalation_model=config.modes.review.escalation_model,
            escalation_effort=config.modes.review.escalation_effort,
        ),
        "autonomous": AutonomousSettings(
            max_iterations=config.modes.autonomous.max_iterations,
            claude_model=config.modes.autonomous.claude_model or config.models.claude.default,
            codex_model=config.modes.autonomous.codex_model or config.models.codex.default,
            claude_effort=config.modes.autonomous.claude_effort or config.efforts.claude.default,
            codex_effort=config.modes.autonomous.codex_effort or config.efforts.codex.default,
        ),
        "execute": {"cli": "codex", "model": "", "effort": "", "skip_review": False},
    }
    session = _create_prompt_session(mode_state, ctx)
    while True:
        try:
            prompt_message = lambda: mode_state.get("formatted_prompt", build_prompt_message(Mode.DEFAULT.value))
            task = (session.prompt(prompt_message) or "").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not task:
            continue
        if task.startswith("/"):
            if _handle_shell_command(ctx, task, mode_state):
                return
            continue
        mode = mode_state["mode"]
        if mode == Mode.ANALYSIS:
            settings: AnalysisSettings = mode_state["analysis"]
            ctx.invoke(
                cmd_analysis,
                task=task,
                rounds=settings.rounds,
                claude_model=settings.claude_model,
                codex_model=settings.codex_model,
                claude_effort=settings.claude_effort,
                codex_effort=settings.codex_effort,
                escalation_model=settings.escalation_model,
                escalation_effort=settings.escalation_effort,
            )
        elif mode == Mode.QUICK_EXECUTE:
            execute_settings = mode_state["execute"]
            run_config = deepcopy(_require_config(ctx))
            run_config.routing.worker = execute_settings["cli"]
            phase_override = run_config.routing.phases.get("executing")
            if not phase_override:
                from .config import PhaseRoutingOverride

                phase_override = PhaseRoutingOverride()
                run_config.routing.phases["executing"] = phase_override
            phase_override.cli = execute_settings["cli"]
            if execute_settings["model"]:
                phase_override.model = execute_settings["model"]
            if execute_settings["effort"]:
                phase_override.reasoning_effort = execute_settings["effort"]
            _start_run(
                ctx,
                task,
                True,
                skip_scoping=True,
                start_at="executing",
                mode=Mode.QUICK_EXECUTE.value,
                skip_review=bool(execute_settings["skip_review"]),
                config=run_config,
            )
        elif mode == Mode.REVIEW:
            review_settings: ReviewSettings = mode_state["review"]
            review_config = deepcopy(_require_config(ctx))
            review_config.models.claude.default = review_settings.claude_model
            review_config.efforts.claude.default = review_settings.claude_effort
            review_config.models.codex.default = review_settings.codex_model
            review_config.efforts.codex.default = review_settings.codex_effort
            if review_settings.escalation_model:
                review_config.models.debate.escalated_claude = review_settings.escalation_model
            if review_settings.escalation_effort:
                review_config.efforts.debate.escalated_claude = review_settings.escalation_effort
            engine = _build_engine(ctx, config=review_config, review_rounds=review_settings.rounds)
            run_id = str(uuid4())
            state = engine.start(
                task,
                run_id,
                is_workspace=True,
                workspace_repos=[],
                start_at="reviewing",
                plan=_synthetic_plan(task),
                mode=Mode.REVIEW.value,
            )
            state = _drive_interactive_approvals(ctx, state.run_id)
            _render_run_snapshot(ctx, state.run_id, state=state)
        elif mode == Mode.AUTONOMOUS:
            auto_settings: AutonomousSettings = mode_state["autonomous"]
            auto_config = deepcopy(_require_config(ctx))
            auto_config.approval.require_plan_approval = False
            auto_config.approval.require_merge_approval = False
            auto_config.models.claude.default = auto_settings.claude_model
            auto_config.efforts.claude.default = auto_settings.claude_effort
            auto_config.models.codex.default = auto_settings.codex_model
            auto_config.efforts.codex.default = auto_settings.codex_effort
            _start_run(
                ctx,
                task,
                False,
                skip_scoping=False,
                mode=Mode.AUTONOMOUS.value,
                config=auto_config,
                autonomous_max_iterations=auto_settings.max_iterations,
            )
        else:
            _start_run(ctx, task, True, skip_scoping=False)


def _handle_shell_command(ctx: click.Context, command: str, mode_state: dict[str, Any] | None = None) -> bool:
    parts = command.split()
    name = parts[0].lower()
    ui = ctx.obj["ui"]
    if name in {"/quit", "/exit"}:
        return True
    if name == "/help":
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="cyan")
        table.add_column(style="dim")
        table.add_row("/help", "Show this help")
        table.add_row("/quit, /exit", "Exit shell")
        table.add_row("/runs", "List pipeline runs")
        table.add_row("/sessions", "List analysis sessions")
        table.add_row("/continue [id]", "Continue a run or analysis session")
        table.add_row("/resume [id]", "Alias for /continue")
        table.add_row("/status [id]", "Show run status")
        table.add_row("/approve [id] <gate>", "Approve a pending gate")
        table.add_row("/reject [id] <gate>", "Reject with feedback")
        table.add_row("/config", "Configure current mode settings")
        table.add_row("/settings", "Show current configuration")
        table.add_row("/mode [name]", "Switch or show mode")
        table.add_row("", "")
        table.add_row("Shift+Tab", "Cycle mode")
        table.add_row("Alt+Enter", "Multiline input")
        ui.stderr_console.print(table)
        return False
    if name == "/config":
        if mode_state is None:
            ui.warning("Mode settings unavailable in this context.")
            return False
        _configure_mode_settings(ctx, mode_state)
        return False
    if name == "/settings":
        if mode_state is None:
            ui.info("mode=default")
            return False
        ui.info(_mode_settings_summary(mode_state))
        return False
    if name == "/mode":
        modes = list(Mode)
        current = mode_state.get("mode", Mode.DEFAULT) if mode_state is not None else Mode.DEFAULT
        if len(parts) == 1:
            ui.info(f"current mode: {current.value}")
            return False
        target = parts[1].lower()
        target_mode = next((mode for mode in modes if mode.value == target), None)
        if target_mode is None:
            ui.warning(f"Unknown mode: {target}. Valid: {', '.join(mode.value for mode in modes)}")
            return False
        if mode_state is not None:
            mode_state["mode"] = target_mode
            mode_state["formatted_prompt"] = build_prompt_message(target_mode.value)
        ui.info(f"mode: {_settings_mode_label(target_mode)}")
        return False
    if name == "/runs":
        ctx.invoke(cmd_status, run_id=None, watch=False)
        return False
    if name == "/sessions":
        ctx.invoke(cmd_sessions, mode_filter="all")
        return False
    if name in {"/continue", "/resume"}:
        ctx.invoke(cmd_continue, session_id=(parts[1] if len(parts) > 1 else None), follow_up=" ".join(parts[2:]) if len(parts) > 2 else None)
        return False
    if name == "/status":
        ctx.invoke(cmd_status, run_id=parts[1] if len(parts) > 1 else "latest", watch=False)
        return False
    if name == "/approve":
        if len(parts) == 2:
            ctx.invoke(cmd_approve, run_id=None, gate=parts[1], force=False, decision=None)
            return False
        if len(parts) >= 3:
            ctx.invoke(cmd_approve, run_id=parts[1], gate=parts[2], force=False, decision=None)
            return False
        ui.warning("Usage: /approve [id] <gate>")
        return False
    if name == "/reject":
        if len(parts) >= 3:
            if parts[1] in {"scope", "plan"}:
                ctx.invoke(
                    cmd_reject,
                    run_id=None,
                    gate=parts[1],
                    reason=" ".join(parts[2:]),
                    full_reject=False,
                )
                return False
            ctx.invoke(
                cmd_reject,
                run_id=parts[1],
                gate=parts[2],
                reason=" ".join(parts[3:]) if len(parts) > 3 else "",
                full_reject=False,
            )
            return False
        ui.warning("Usage: /reject [id] <gate> <reason>")
        return False
    if name == "/logs":
        ctx.invoke(cmd_logs, run_id=(parts[1] if len(parts) > 1 else None), step=None, tail=40)
        return False
    ui.warning(f"Unknown shell command: {command}")
    return False


@main.command("approve")
@click.argument("run_id", required=False, default=None)
@click.argument("gate", required=False, type=click.Choice(["scope", "plan"]))
@click.option("--force", is_flag=True, default=False, help="Reserved for backward compatibility.")
@click.option(
    "--decision",
    type=click.Choice(["approve_claude", "approve_codex", "override"]),
    default=None,
    help="Gate-specific decision for debate gates.",
)
@click.pass_context
def cmd_approve(
    ctx: click.Context,
    run_id: str | None,
    gate: str | None,
    force: bool,
    decision: str | None,
) -> None:
    """Approve a pending gate."""
    if run_id in {"scope", "plan"} and gate is None:
        gate = run_id
        run_id = None
    if gate is None:
        raise click.UsageError("Missing argument: GATE")
    engine = _build_engine(ctx)
    run_id = _resolve_run_id_arg(ctx, run_id)
    try:
        state = engine.approve(run_id, gate, force=force, decision=decision)
    except EngineError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_run_snapshot(ctx, state.run_id, state=state)


@main.command("reject")
@click.argument("run_id", required=False, default=None)
@click.argument("gate", required=False, type=click.Choice(["scope", "plan"]))
@click.option("--reason", required=True)
@click.option("--full", "full_reject", is_flag=True, default=False, help="Terminate instead of requesting another plan.")
@click.pass_context
def cmd_reject(ctx: click.Context, run_id: str | None, gate: str | None, reason: str, full_reject: bool) -> None:
    """Reject a pending gate with feedback."""
    if run_id in {"scope", "plan"} and gate is None:
        gate = run_id
        run_id = None
    if gate is None:
        raise click.UsageError("Missing argument: GATE")
    engine = _build_engine(ctx)
    run_id = _resolve_run_id_arg(ctx, run_id)
    try:
        state = engine.reject(run_id, gate, reason, full=full_reject)
    except EngineError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_run_snapshot(ctx, state.run_id, state=state)


@main.command("status")
@click.argument("run_id", required=False)
@click.option("--watch", is_flag=True, default=False, help="Refresh until the run reaches a terminal state.")
@click.option("--refresh-interval", default=1.0, show_default=True, type=float)
@click.pass_context
def cmd_status(ctx: click.Context, run_id: str | None, watch: bool, refresh_interval: float) -> None:
    """Show run status for the active repository."""
    _ensure_runtime_gitignore(ctx)
    state_mgr = StateManager(ctx.obj["artifact_root"])
    ui = ctx.obj["ui"]

    if run_id is None:
        states = [state_mgr.load(current_run_id) for current_run_id in state_mgr.list_runs()]
        ui.console.print(ui.render_runs_overview(states))
        return

    run_id = _resolve_run_id_arg(ctx, run_id)

    if watch:
        ui.watch(
            lambda: _render_status(ctx, run_id),
            stop_when=lambda: state_mgr.load(run_id).status in TERMINAL_STATES,
            refresh_per_second=max(1.0, 1.0 / max(refresh_interval, 0.1)),
        )
        return

    ui.console.print(_render_status(ctx, run_id))
    _print_commit_suggestions_if_needed(ctx, state_mgr.load(run_id))


@main.command("logs")
@click.argument("run_id", required=False, default=None)
@click.argument("step", required=False, type=int)
@click.option("--tail", default=40, show_default=True, type=int)
@click.pass_context
def cmd_logs(ctx: click.Context, run_id: str | None, step: int | None, tail: int) -> None:
    """View run event logs or a step-result artifact."""
    artifact_root = ctx.obj["artifact_root"]
    ui = ctx.obj["ui"]
    run_id = _resolve_run_id_arg(ctx, run_id)
    if step is None:
        path = artifact_root / "logs" / f"run-{run_id}.log"
    else:
        matches = sorted((artifact_root / "results").glob(f"step-{step}-{run_id[:8]}-*.json"))
        path = matches[-1] if matches else artifact_root / "results" / f"pending-step-{step}.json"

    if not path.exists():
        raise click.ClickException(f"No log found at {path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    ui.print_logs("\n".join(lines[-tail:]), title=path.name)


main.add_command(cmd_logs, "log")


@main.command("show")
@click.argument("run_id", required=False, default=None)
@click.argument("artifact", required=False, default="plan", type=click.Choice(["plan"]))
@click.pass_context
def cmd_show(ctx: click.Context, run_id: str | None, artifact: str) -> None:
    """Show a full artifact for a run."""
    valid_artifacts = {"plan"}
    if run_id in valid_artifacts and artifact == "plan":
        run_id = None
        artifact = "plan"
    resolved_run_id = _resolve_run_id_arg(ctx, run_id)
    state = StateManager(ctx.obj["artifact_root"]).load(resolved_run_id)
    store = ArtifactStore(ctx.obj["artifact_root"])

    if artifact == "plan":
        if not state.plan_id:
            raise click.ClickException(f"Run {resolved_run_id} does not have a saved plan yet.")
        ctx.obj["ui"].print_plan(
            _load_plan_artifact(store, state.plan_id),
            run_id=resolved_run_id,
            detailed=True,
        )
        return

    raise click.ClickException(f"Unsupported artifact: {artifact}")


@main.command("doctor")
@click.option("--fix", "fix_mode", is_flag=True, default=False, help="Attempt to fix common issues and re-check.")
@click.pass_context
def cmd_doctor(ctx: click.Context, fix_mode: bool) -> None:
    """Run installation and environment checks."""
    if fix_mode:
        report, actions = run_doctor_fix(
            ctx.obj["repo_root"],
            ctx.obj["artifact_root"],
            ctx.obj["config"],
        )
        if actions:
            ctx.obj["ui"].print_doctor_fix_actions(actions)
    else:
        report = run_doctor(
            ctx.obj["repo_root"],
            ctx.obj["artifact_root"],
            ctx.obj["config"],
        )
    ctx.obj["ui"].print_doctor_report(report)
    if report.overall_status == "pass":
        ctx.obj["ui"].print_doctor_ready()


@main.command("update")
@click.pass_context
def cmd_self_update(ctx: click.Context) -> None:
    """Pull the latest source and reinstall."""
    ui = ctx.obj["ui"]
    meta = read_install_meta()
    mode = meta.get("install_mode", "")
    source = meta.get("source_repo_path", "")

    try:
        if mode in {"local-pipx", "editable"} and source:
            ui.info(f"Pulling from {source} ...")
            subprocess.run(["git", "-C", source, "pull", "--ff-only"], check=True, shell=False)
            if mode == "local-pipx":
                ui.info("Reinstalling via pipx ...")
                subprocess.run(["pipx", "install", "--force", source], check=True, shell=False)
            else:
                ui.info("Editable install — source updated, no reinstall needed.")
        elif mode in {"pypi", "pip-user"} or not source:
            try:
                subprocess.run(["pipx", "upgrade", "ai-orchestrator"], check=True, shell=False)
            except (FileNotFoundError, subprocess.CalledProcessError):
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade", "ai-orchestrator"],
                    check=True,
                    shell=False,
                )
        else:
            raise click.ClickException("Cannot determine install source. Re-run the install script.")
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        command = " ".join(str(part) for part in exc.cmd)
        raise click.ClickException(f"Command failed: {command}") from exc

    ui.info("Update complete. Run `orch doctor` to verify.")


@main.command("sync")
@click.pass_context
def cmd_sync(ctx: click.Context) -> None:
    """Refresh workspace AI configs for the current repository."""
    ui = ctx.obj["ui"]
    actions = refresh_workflow(ctx.obj["repo_root"])
    if actions:
        ui.print_file_updates(actions)
    ui.info("Refreshing reviewer config from repo heuristics and bundled rules ...")
    ctx.invoke(cmd_review_analyze)
    ui.info("Workspace sync complete.")


@main.command("install-shell")
@click.option("--shell", type=click.Choice(["bash", "zsh", "fish", "powershell", "pwsh"]))
@click.option("--force", is_flag=True, default=False, help="Rewrite integration even if it already exists.")
@click.pass_context
def cmd_install_shell(ctx: click.Context, shell: str | None, force: bool) -> None:
    """Install shell integration and the `aio` compatibility alias."""
    destination = install_shell_integration(shell=shell, force=force)
    shell_name = shell or ("powershell" if destination.suffix.lower() == ".ps1" else destination.suffix.lstrip("."))
    ctx.obj["ui"].print_install_shell_result(shell_name, destination)


@main.command("review-install")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing reviewer config.")
@click.pass_context
def cmd_review_install(ctx: click.Context, force: bool) -> None:
    """Install repository-local reviewer config and bundled rules."""
    _ensure_runtime_gitignore(ctx)
    config_path = ctx.obj["repo_root"] / ".ai-review" / "config.json"
    if config_path.exists() and not force:
        raise click.ClickException(
            "Reviewer config already exists. Use 'orch review-analyze' to refresh, "
            "or 'orch review-install --force' to overwrite."
        )
    result = install_reviewer(ctx.obj["repo_root"])
    _print_review_setup_result(ctx, result)


@main.command("review-analyze")
@click.pass_context
def cmd_review_analyze(ctx: click.Context) -> None:
    """Refresh reviewer config from the current repository structure."""
    _ensure_runtime_gitignore(ctx)
    result = analyze_repo(ctx.obj["repo_root"])
    _print_review_setup_result(ctx, result)


@main.command("clean")
@click.option("--all", "clean_all", is_flag=True, default=False)
@click.pass_context
def cmd_clean(ctx: click.Context, clean_all: bool) -> None:
    """Remove completed run artifacts and orphaned worktrees."""
    _ensure_runtime_gitignore(ctx)
    artifact_root = ctx.obj["artifact_root"]
    state_mgr = StateManager(artifact_root)
    store = ArtifactStore(artifact_root)
    worktrees = WorktreeManager(ctx.obj["repo_root"], artifact_root)

    removable_statuses = {"DONE", "FAILED"} if not clean_all else None
    live_run_ids: set[str] = set()
    removed_runs = 0

    for run_id in state_mgr.list_runs():
        state = state_mgr.load(run_id)
        if removable_statuses is not None and state.status not in removable_statuses:
            live_run_ids.add(run_id)
            continue
        if state.worktree_path and state.worktree_branch:
            try:
                worktrees.remove(Path(state.worktree_path), state.worktree_branch, force=True)
            except Exception:
                pass
        for path in store.list_run_artifacts(run_id):
            if path.is_file():
                path.unlink(missing_ok=True)
        (artifact_root / "state" / f"run-{run_id}.json").unlink(missing_ok=True)
        (artifact_root / "state" / f"run-{run_id}.lock").unlink(missing_ok=True)
        removed_runs += 1

    for orphan in store.orphaned_worktrees(live_run_ids):
        try:
            worktrees.remove(orphan, f"{ctx.obj['config'].worktree.branch_prefix}{orphan.name}", force=True)
        except Exception:
            pass

    ctx.obj["ui"].info(f"Removed {removed_runs} completed run(s).")


@main.command("config")
@click.pass_context
def cmd_config(ctx: click.Context) -> None:
    """Show the effective orchestrator configuration."""
    config = _require_config(ctx)
    text = json.dumps(config, default=lambda value: value.__dict__, indent=2, sort_keys=True)
    ctx.obj["ui"].print_logs(text, title="Effective Config")


def _drive_interactive_approvals(ctx: click.Context, run_id: str) -> RunState:
    engine = _build_engine(ctx)
    state_mgr = StateManager(ctx.obj["artifact_root"])
    ui = ctx.obj["ui"]
    while True:
        state = state_mgr.load(run_id)
        if state.status != "PAUSED":
            return state
        if state.current_phase == "SCOPING":
            gate = "scope"
        elif state.current_phase == "APPROVAL_PLAN":
            gate = "plan"
        else:
            return state
        if gate == "scope":
            responses: dict[str, str] = {}
            for ai_name, reference in (state.ai_scope_refs or {}).items():
                try:
                    responses[ai_name] = ArtifactStore(ctx.obj["artifact_root"]).read_text(reference)
                except Exception:
                    continue
            if responses:
                ui.print_scoping_conversation(responses, state.scoping_round or 1)
            ui.print_scoping_result(
                {
                    "normalized_task": state.normalized_task or state.task,
                    "complexity_tier": state.complexity_tier or "unknown",
                    "blocking_reason": state.error,
                }
            )
        elif gate == "plan" and state.plan_id:
            plan = _load_plan_artifact(ArtifactStore(ctx.obj["artifact_root"]), state.plan_id)
            ui.print_plan(plan, run_id=state.run_id, detailed=True)
        if gate == "plan":
            ui.print_execution_info(engine.resolve_execution_settings(state))
            choice = ui.approval_choice(
                gate,
                (
                    f"Run {run_id[:8]} has a plan ready.\n\n"
                    "  [bold]Approve[/bold]          Start building with this plan\n"
                    "  [bold]Request changes[/bold]  Send feedback — Claude will revise\n"
                    "  [bold]Reject[/bold]           Stop this run\n"
                    "  [bold]Adjust settings[/bold]  Change executor, model, or effort\n"
                    "  [bold]Reassess complexity[/bold]  Override complexity tier"
                ),
                choices=["approve", "soft-reject", "full-reject", "adjust", "reassess-complexity"],
                default="approve",
            )
            if choice == "adjust":
                _adjust_execution_settings(ui, engine, state_mgr, state, run_id)
                continue
            if choice == "reassess-complexity":
                tier = _select_choice(
                    "Complexity tier:",
                    ["simple", "moderate", "complex", "architectural", "extramax"],
                    default=(state.complexity_tier or "moderate"),
                )
                if tier:
                    state.execution_overrides["complexity_tier"] = tier
                    state.complexity_tier = tier
                    state_mgr.save(state)
                continue
            if choice == "approve":
                state = engine.approve(run_id, gate)
            elif choice == "full-reject":
                state = engine.reject(run_id, gate, "Plan rejected by user", full=True)
            else:
                reason = ui.rejection_reason("Rejected in interactive mode")
                state = engine.reject(run_id, gate, reason, full=False)
            continue
        scope_choice = ui.approval_choice(
            gate,
            (
                f"Run {run_id[:8]} has finished scoping.\n\n"
                "  [bold]Accept scope & plan[/bold]   Proceed to planning\n"
                "  [bold]Reply to AIs[/bold]          Continue scoping with feedback\n"
                "  [bold]Adjust complexity[/bold]     Override complexity tier\n"
                "  [bold]Reject[/bold]                Stop this run"
            ),
            choices=["accept", "reply", "adjust-complexity", "reject"],
            default="accept",
        )
        if scope_choice == "accept":
            state = engine.approve(run_id, gate)
        elif scope_choice == "reply":
            reason = ui.rejection_reason("Provide scoping feedback")
            state = engine.reject(run_id, gate, reason)
        elif scope_choice == "adjust-complexity":
            tier = _select_choice(
                "Complexity tier:",
                ["simple", "moderate", "complex", "architectural", "extramax"],
                default=(state.complexity_tier or "moderate"),
            )
            if tier:
                state.execution_overrides["complexity_tier"] = tier
                state.complexity_tier = tier
                state_mgr.save(state)
            continue
        else:
            state = engine._terminate_run(state, "Scope rejected by user")


def _render_status(ctx: click.Context, run_id: str) -> RenderableType:
    state_mgr = StateManager(ctx.obj["artifact_root"])
    state = state_mgr.load(run_id)
    store = ArtifactStore(ctx.obj["artifact_root"])
    plan = _load_plan_artifact(store, state.plan_id) if state.plan_id else None
    step_results = [store.read_json(reference) for reference in state.step_results]
    log_entries = _load_log_entries(ctx.obj["artifact_root"] / "logs" / f"run-{run_id}.log")
    return ctx.obj["ui"].render_status(state, plan=plan, step_results=step_results, log_entries=log_entries)


def _render_run_snapshot(ctx: click.Context, run_id: str, *, state=None) -> None:
    if state is None:
        state = StateManager(ctx.obj["artifact_root"]).load(run_id)
    store = ArtifactStore(ctx.obj["artifact_root"])
    if state.current_phase == "SCOPING":
        ctx.obj["ui"].print_scoping_result(
            {
                "normalized_task": state.normalized_task or state.task,
                "complexity_tier": state.complexity_tier or "unknown",
                "blocking_reason": state.error,
            }
        )
    if state.current_phase == "APPROVAL_PLAN" and state.plan_id:
        ctx.obj["ui"].print_plan(_load_plan_artifact(store, state.plan_id), run_id=state.run_id)
    ctx.obj["ui"].print_status(
        state,
        plan=_load_plan_artifact(store, state.plan_id) if state.plan_id else None,
        step_results=[store.read_json(reference) for reference in state.step_results],
        log_entries=_load_log_entries(ctx.obj["artifact_root"] / "logs" / f"run-{run_id}.log"),
    )
    _print_commit_suggestions_if_needed(ctx, state)


def _print_commit_suggestions_if_needed(ctx: click.Context, state: RunState) -> None:
    if state.status == "DONE" and state.commit_commands:
        ctx.obj["ui"].print_commit_suggestions(state.commit_commands)


def _load_log_entries(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _print_review_setup_result(ctx: click.Context, result: dict) -> None:
    summary = result["summary"]
    stack = ", ".join(summary["stack"]) or "<none detected>"
    workspaces = ", ".join(summary["workspaces"]) or "<none>"
    architecture = ", ".join(summary["architecture_patterns"]) or "<none detected>"
    commands = summary["commands"]
    command_lines = [f"{name}: {value}" for name, value in commands.items()]
    refinement_lines = summary["manual_refinement_needed"] or ["<none>"]

    click.echo(f"Reviewer {result['action']}.")
    click.echo(f"Config: {Path(result['config_path']).relative_to(ctx.obj['repo_root'])}")
    click.echo(f"Rules: {Path(result['rules_path']).relative_to(ctx.obj['repo_root'])}")
    click.echo(f"Stack: {stack}")
    click.echo(f"Workspaces: {workspaces}")
    click.echo(f"Architecture: {architecture}")
    click.echo("Commands:")
    for line in command_lines or ["<none detected>"]:
        click.echo(f"  {line}")
    click.echo("Manual refinement:")
    for line in refinement_lines:
        click.echo(f"  {line}")


def _run_diff_stat(repo_root: Path, base_commit: str, branch_name: str) -> str:
    completed = subprocess.run(
        ["git", "diff", "--stat", f"{base_commit}...{branch_name}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        return completed.stderr.strip() or completed.stdout.strip()
    return completed.stdout.strip()


if __name__ == "__main__":
    main()
