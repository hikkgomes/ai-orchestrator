"""Repository bootstrap and shell integration helpers."""

from __future__ import annotations

import json as _json
import os
from pathlib import Path


DEFAULT_CONFIG = """[orchestrator]
max_retries = 3
watchdog_timeout = 3600

[routing]
planner = "claude"
worker = "codex"
reviewer = "claude"
adjudicator = "codex"
feasibility_checker = "codex"
scoper = "claude"

[routing.claude]
model = ""
reasoning_effort = "high"

[routing.codex]
model = ""
reasoning_effort = "medium"

[routing.phases.scoping]
reasoning_effort = "high"

[routing.phases.planning]
model_simple = "claude-sonnet-4-5-20250514"
model_moderate = "claude-sonnet-4-5-20250514"
model_complex = "claude-opus-4-5-20250514"
model_architectural = "claude-opus-4-5-20250514"

[routing.phases.reviewing]
reasoning_effort = "high"

[scoping]
enabled = true

[feasibility]
enabled = true
max_feasibility_replans = 2

[debate]
escalated_claude_model = "claude-opus-4-5-20250514"
escalated_claude_effort = "max"
escalated_codex_effort = "xhigh"

[sessions]
enable_planning_resume = true
enable_review_resume = true

[approval]
require_plan_approval = true
require_merge_approval = true

[worktree]
base_branch = "main"
branch_prefix = "aio/"

# Uncomment for multi-repo workspace mode:
# [workspace]
# repos = ["frontend", "backend"]

[logging]
retain_raw_output = false
retain_prompts = false

[cli_compat]
claude_min_version = ""
codex_min_version = ""
"""

DEFAULT_WORKFLOW = """# Default workflow configuration for ai-orchestrator.
#
# This file is the authoritative workflow definition for phase structure and
# default phase-level settings. The engine loads this file at startup.
# aio.toml provides user-level overrides for supported settings.

name: default
description: >
  Full orchestrated run: scope -> plan -> feasibility -> execute -> review -> adjudicate -> merge.

phases:
  scoping:
    cli: claude
    retries: 2

  planning:
    cli: claude
    approval_gate: plan
    retries: 3

  feasibility:
    cli: codex
    worktree: true
    retries: 2

  executing:
    cli: codex
    worktree: true
    retries: 3

  reviewing:
    cli: claude
    retries: 3

  adjudicating:
    cli: codex
    retries: 3

  merging:
    # Applies changes as a staged diff via git merge --squash; never commits.
    # No approval gate — triggered automatically after adjudication passes.
"""

GITIGNORE_MARKER = "# ai-orchestrator runtime artifacts"
RUNTIME_GITIGNORE_ENTRIES = (".ai-orchestrator/", ".ai-review/")
GITIGNORE_BLOCK = GITIGNORE_MARKER + "\n" + "\n".join(RUNTIME_GITIGNORE_ENTRIES) + "\n"


def scaffold_repository(repo_root: Path, *, force: bool = False) -> list[tuple[str, str]]:
    """Create default config and ignore files in *repo_root*."""
    actions: list[tuple[str, str]] = []
    config_path = repo_root / "aio.toml"
    workflow_path = repo_root / "workflows" / "default.yaml"
    gitignore_path = repo_root / ".gitignore"

    config_existed = config_path.exists()
    if force or not config_existed:
        config_content = DEFAULT_CONFIG
        workspace_repos = _detect_workspace_repos(repo_root)
        if workspace_repos:
            repos_value = ", ".join(f'"{repo}"' for repo in workspace_repos)
            config_content = (
                DEFAULT_CONFIG.rstrip()
                + "\n\n[workspace]\n"
                + f"repos = [{repos_value}]\n"
            )
        _write_text(config_path, config_content)
        actions.append(("updated" if config_existed else "created", "aio.toml"))

    workflow_existed = workflow_path.exists()
    if force or not workflow_existed:
        _write_text(workflow_path, DEFAULT_WORKFLOW)
        actions.append(("updated" if workflow_existed else "created", "workflows/default.yaml"))

    gitignore_action = ensure_runtime_gitignore(repo_root)
    if gitignore_action:
        actions.append((gitignore_action, ".gitignore"))

    return actions


def ensure_runtime_gitignore(repo_root: Path) -> str | None:
    """Ensure app-owned runtime artifacts are ignored in *repo_root*.

    Returns ``"created"``, ``"updated"``, or ``None`` when no write was needed.
    """
    gitignore_path = repo_root / ".gitignore"
    existed = gitignore_path.exists()
    current = gitignore_path.read_text(encoding="utf-8") if existed else ""
    updated = render_runtime_gitignore(current)
    if updated == current:
        return None
    _write_text(gitignore_path, updated)
    return "updated" if current else "created"


def refresh_workflow(repo_root: Path) -> list[tuple[str, str]]:
    """Update ``workflows/default.yaml`` when it differs from the bundled default."""
    workflow_path = repo_root / "workflows" / "default.yaml"
    if not workflow_path.exists():
        return []
    current = workflow_path.read_text(encoding="utf-8")
    if current == DEFAULT_WORKFLOW:
        return []
    _write_text(workflow_path, DEFAULT_WORKFLOW)
    return [("updated", "workflows/default.yaml")]


def install_shell_integration(
    *,
    shell: str | None = None,
    home_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Install shell integration for the current user."""
    target_shell = (shell or detect_shell()).lower()
    home = home_dir or Path.home()

    if target_shell in {"bash", "zsh"}:
        rc_path = home / (".bashrc" if target_shell == "bash" else ".zshrc")
        integration_dir = home / ".config" / "ai-orchestrator" / "shell"
        integration_path = integration_dir / f"orch.{target_shell}"
        _write_text(integration_path, _shell_snippet(target_shell))
        source_line = f'[ -f "{integration_path}" ] && source "{integration_path}"'
        _append_line_once(rc_path, source_line, force=force)
        return integration_path

    if target_shell == "fish":
        integration_path = home / ".config" / "fish" / "conf.d" / "orch.fish"
        _write_text(integration_path, _shell_snippet(target_shell))
        return integration_path

    if target_shell in {"powershell", "pwsh"}:
        integration_path = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
        _append_line_once(integration_path, _shell_snippet("powershell"), force=force)
        return integration_path

    raise ValueError(f"Unsupported shell: {target_shell}")


def detect_shell() -> str:
    """Best-effort shell detection."""
    shell = Path(os.environ.get("SHELL", "")).name.lower()
    if shell:
        return shell
    if os.name == "nt":
        return "powershell"
    return "bash"


def _shell_snippet(shell: str) -> str:
    if shell == "bash":
        return """# ai-orchestrator shell integration
alias aio=orch
eval "$(_ORCH_COMPLETE=bash_source orch)"
"""
    if shell == "zsh":
        return """# ai-orchestrator shell integration
alias aio=orch
autoload -Uz compinit
compinit
eval "$(_ORCH_COMPLETE=zsh_source orch)"
"""
    if shell == "fish":
        return """# ai-orchestrator shell integration
alias aio orch
_ORCH_COMPLETE=fish_source orch | source
"""
    if shell == "powershell":
        return """# ai-orchestrator shell integration
Set-Alias aio orch
"""
    raise ValueError(f"Unsupported shell: {shell}")


def _append_line_once(path: Path, line: str, *, force: bool) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if not force and line in existing:
        return
    content = existing.rstrip()
    if content:
        content += "\n"
    content += line.rstrip() + "\n"
    _write_text(path, content)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_runtime_gitignore(content: str) -> str:
    if _has_runtime_ignore(content) and GITIGNORE_MARKER not in content:
        return content

    block_lines = GITIGNORE_BLOCK.rstrip("\n").splitlines()
    lines = content.splitlines()
    marker_index = next(
        (index for index, line in enumerate(lines) if line.strip() == GITIGNORE_MARKER),
        None,
    )
    if marker_index is not None:
        end_index = marker_index + 1
        while end_index < len(lines) and lines[end_index].strip():
            end_index += 1
        next_lines = lines[:marker_index] + block_lines + lines[end_index:]
        return "\n".join(next_lines).rstrip() + "\n"

    trimmed = content.rstrip()
    if trimmed:
        trimmed += "\n\n"
    return trimmed + GITIGNORE_BLOCK


def _has_runtime_ignore(content: str) -> bool:
    ignored_roots: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("/"):
            stripped = stripped[1:]
        if stripped.endswith("/**"):
            stripped = stripped[:-3]
        stripped = stripped.rstrip("/")
        ignored_roots.add(stripped)

    required_roots = {entry.rstrip("/") for entry in RUNTIME_GITIGNORE_ENTRIES}
    return required_roots.issubset(ignored_roots)


def _detect_workspace_repos(repo_root: Path) -> list[str]:
    if (repo_root / ".git").exists():
        return []
    return [
        path.name
        for path in sorted(repo_root.iterdir())
        if path.is_dir() and (path / ".git").exists()
    ]


def _global_config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ai-orchestrator"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ai-orchestrator"


_GLOBAL_CONFIG_DIR = _global_config_dir()
_INSTALL_META = _GLOBAL_CONFIG_DIR / "install-meta.json"


def read_install_meta() -> dict:
    """Return install metadata, or {} if absent/corrupt."""
    try:
        return _json.loads(_INSTALL_META.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, _json.JSONDecodeError):
        return {}


def write_install_meta(source_repo_path: str, install_mode: str) -> None:
    """Persist install metadata for CLI update flows."""
    _GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _INSTALL_META.write_text(
        _json.dumps({"source_repo_path": source_repo_path, "install_mode": install_mode}),
        encoding="utf-8",
    )


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_WORKFLOW",
    "GITIGNORE_BLOCK",
    "detect_shell",
    "ensure_runtime_gitignore",
    "install_shell_integration",
    "render_runtime_gitignore",
    "read_install_meta",
    "refresh_workflow",
    "scaffold_repository",
    "write_install_meta",
]
