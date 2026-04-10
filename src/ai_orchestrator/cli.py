"""Click CLI entry point for ai-orchestrator."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import click
from rich.console import RenderableType

from . import __version__
from .artifacts import ArtifactStore
from .bootstrap import install_shell_integration, read_install_meta, scaffold_repository
from .config import Config, ConfigError, load_config
from .doctor import run_doctor
from .engine import Engine, EngineError
from .models import RunState
from .reviewer.installer import analyze_repo, install_reviewer
from .state import StateError, StateManager
from .ui import OrchestratorUI, TERMINAL_STATES
from .worktree import WorktreeManager


def _build_engine(ctx: click.Context) -> Engine:
    return Engine(
        _require_config(ctx),
        ctx.obj["repo_root"],
        ctx.obj["artifact_root"],
        ui=ctx.obj["ui"],
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


def _resolve_run_id_arg(ctx: click.Context, run_id: str) -> str:
    state_mgr = StateManager(ctx.obj["artifact_root"])
    try:
        return state_mgr.resolve_run_id(run_id)
    except StateError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="orch")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Coordinate Claude Code and Codex as stateless workers."""
    ctx.ensure_object(dict)
    repo_root = Path.cwd()
    artifact_root = repo_root / ".ai-orchestrator"
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


def _start_run(ctx: click.Context, task: str, interactive: bool, *, skip_scoping: bool) -> None:
    if skip_scoping:
        _require_config(ctx).scoping.enabled = False
    engine = _build_engine(ctx)
    run_id = str(uuid4())
    workspace_repos = ctx.obj["workspace_repos"]
    state = engine.start(
        task,
        run_id,
        is_workspace=bool(workspace_repos),
        workspace_repos=workspace_repos,
    )
    if interactive:
        state = _drive_interactive_approvals(ctx, state.run_id)
    _render_run_snapshot(ctx, state.run_id, state=state)


@main.command("new")
@click.argument("task")
@click.option(
    "--interactive/--no-interactive",
    default=True,
    help="Drive approval gates inline instead of returning control at the first pause.",
)
@click.option("--detach", is_flag=True, default=False, help="Alias for --no-interactive.")
@click.option("--skip-scoping", is_flag=True, default=False)
@click.pass_context
def cmd_new(ctx: click.Context, task: str, interactive: bool, detach: bool, skip_scoping: bool) -> None:
    """Start a new orchestrated run for TASK."""
    interactive = interactive and not detach
    _start_run(ctx, task, interactive, skip_scoping=skip_scoping)


@main.command("run")
@click.argument("task")
@click.option(
    "--interactive/--no-interactive",
    default=True,
    help="Drive approval gates inline instead of returning control at the first pause.",
)
@click.option("--detach", is_flag=True, default=False, help="Alias for --no-interactive.")
@click.option("--skip-scoping", is_flag=True, default=False)
@click.pass_context
def cmd_run(ctx: click.Context, task: str, interactive: bool, detach: bool, skip_scoping: bool) -> None:
    """Start a new orchestrated run for TASK."""
    interactive = interactive and not detach
    _start_run(ctx, task, interactive, skip_scoping=skip_scoping)


@main.command("resume")
@click.argument("run_id")
@click.pass_context
def cmd_resume(ctx: click.Context, run_id: str) -> None:
    """Resume a paused or crashed run."""
    engine = _build_engine(ctx)
    run_id = _resolve_run_id_arg(ctx, run_id)
    try:
        state = engine.resume(run_id)
    except EngineError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_run_snapshot(ctx, state.run_id, state=state)


@main.command("approve")
@click.argument("run_id")
@click.argument("gate", type=click.Choice(["scope", "plan"]))
@click.option("--force", is_flag=True, default=False, help="Reserved for backward compatibility.")
@click.pass_context
def cmd_approve(ctx: click.Context, run_id: str, gate: str, force: bool) -> None:
    """Approve a pending gate."""
    engine = _build_engine(ctx)
    run_id = _resolve_run_id_arg(ctx, run_id)
    try:
        state = engine.approve(run_id, gate, force=force)
    except EngineError as exc:
        raise click.ClickException(str(exc)) from exc
    _render_run_snapshot(ctx, state.run_id, state=state)


@main.command("reject")
@click.argument("run_id")
@click.argument("gate", type=click.Choice(["scope", "plan"]))
@click.option("--reason", required=True)
@click.pass_context
def cmd_reject(ctx: click.Context, run_id: str, gate: str, reason: str) -> None:
    """Reject a pending gate with feedback."""
    engine = _build_engine(ctx)
    run_id = _resolve_run_id_arg(ctx, run_id)
    try:
        state = engine.reject(run_id, gate, reason)
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
@click.argument("run_id")
@click.argument("step", required=False, type=int)
@click.option("--tail", default=40, show_default=True, type=int)
@click.pass_context
def cmd_logs(ctx: click.Context, run_id: str, step: int | None, tail: int) -> None:
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
@click.argument("run_id")
@click.argument("artifact", type=click.Choice(["plan"]))
@click.pass_context
def cmd_show(ctx: click.Context, run_id: str, artifact: str) -> None:
    """Show a full artifact for a run."""
    resolved_run_id = _resolve_run_id_arg(ctx, run_id)
    state = StateManager(ctx.obj["artifact_root"]).load(resolved_run_id)
    store = ArtifactStore(ctx.obj["artifact_root"])

    if artifact == "plan":
        if not state.plan_id:
            raise click.ClickException(f"Run {resolved_run_id} does not have a saved plan yet.")
        ctx.obj["ui"].print_plan(
            store.read_json(state.plan_id),
            run_id=resolved_run_id,
            detailed=True,
        )
        return

    raise click.ClickException(f"Unsupported artifact: {artifact}")


@main.command("doctor")
@click.pass_context
def cmd_doctor(ctx: click.Context) -> None:
    """Run installation and environment checks."""
    report = run_doctor(
        ctx.obj["repo_root"],
        ctx.obj["artifact_root"],
        ctx.obj["config"],
    )
    ctx.obj["ui"].print_doctor_report(report)


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
    result = analyze_repo(ctx.obj["repo_root"])
    _print_review_setup_result(ctx, result)


@main.command("clean")
@click.option("--all", "clean_all", is_flag=True, default=False)
@click.pass_context
def cmd_clean(ctx: click.Context, clean_all: bool) -> None:
    """Remove completed run artifacts and orphaned worktrees."""
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
            return engine.resume(run_id)
        if gate == "scope":
            if state.normalized_task or state.complexity_tier:
                ui.print_scoping_result(
                    {
                        "normalized_task": state.normalized_task or state.task,
                        "complexity_tier": state.complexity_tier or "unknown",
                        "blocking_reason": state.error,
                    }
                )
        elif gate == "plan" and state.plan_id:
            plan = ArtifactStore(ctx.obj["artifact_root"]).read_json(state.plan_id)
            ui.print_plan(plan, run_id=state.run_id, detailed=True)
        if ui.approval_prompt(gate, f"Run {run_id} is paused at {gate} approval."):
            state = engine.approve(run_id, gate)
        else:
            reason = ui.rejection_reason("Rejected in interactive mode")
            state = engine.reject(run_id, gate, reason)


def _render_status(ctx: click.Context, run_id: str) -> RenderableType:
    state_mgr = StateManager(ctx.obj["artifact_root"])
    state = state_mgr.load(run_id)
    store = ArtifactStore(ctx.obj["artifact_root"])
    plan = store.read_json(state.plan_id) if state.plan_id else None
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
        ctx.obj["ui"].print_plan(store.read_json(state.plan_id), run_id=state.run_id)
    if state.current_phase == "FEASIBILITY" and state.feasibility_id:
        ctx.obj["ui"].print_feasibility_result(store.read_json(state.feasibility_id))
    ctx.obj["ui"].print_status(
        state,
        plan=store.read_json(state.plan_id) if state.plan_id else None,
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
