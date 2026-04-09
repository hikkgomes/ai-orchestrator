"""Repository bootstrap and shell integration helpers."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_CONFIG = """[orchestrator]
max_retries = 3
max_rework_loops = 3
max_replan_loops = 2
step_timeout = 300
planning_timeout = 120
execution_timeout_low = 180
execution_timeout_medium = 300
execution_timeout_high = 600
review_timeout = 180
adjudication_timeout = 120

[routing]
planner = "claude"
worker = "codex"
reviewer = "claude"
adjudicator = "claude"

[routing.claude]
model = ""
reasoning_effort = "high"

[routing.codex]
model = ""
reasoning_effort = "medium"

[approval]
require_plan_approval = true
require_merge_approval = true

[worktree]
base_branch = "main"
branch_prefix = "aio/"

[logging]
retain_raw_output = false
retain_prompts = false

[cli_compat]
claude_min_version = ""
codex_min_version = ""
"""

DEFAULT_WORKFLOW = """# Default workflow configuration for ai-orchestrator.
#
# This file describes the canonical workflow phases and their routing.
# The authoritative specification is in docs/workflow.md and aio.toml.

name: default
description: >
  Full orchestrated run: plan -> approve -> execute -> review -> adjudicate -> merge.

phases:
  planning:
    cli: claude
    approval_gate: plan
    retries: 3

  executing:
    cli: codex
    worktree: true
    retries: 3
    complexity_timeouts:
      low: 180
      medium: 300
      high: 600

  reviewing:
    cli: claude
    retries: 3

  adjudicating:
    cli: claude
    retries: 3
    loop_limits:
      rework: 3
      replan: 2

  merging:
    approval_gate: merge
    pre_checks:
      - clean_working_tree
      - base_commit_match
"""

GITIGNORE_BLOCK = """# ai-orchestrator runtime artifacts
.ai-orchestrator/state/
.ai-orchestrator/worktrees/
.ai-orchestrator/logs/
.ai-orchestrator/prompts/
.ai-orchestrator/results/
.ai-orchestrator/approvals/
.ai-orchestrator/feedback/
.ai-orchestrator/executions/
.ai-orchestrator/metadata.sqlite3
"""


def scaffold_repository(repo_root: Path, *, force: bool = False) -> list[tuple[str, str]]:
    """Create default config and ignore files in *repo_root*."""
    actions: list[tuple[str, str]] = []
    config_path = repo_root / "aio.toml"
    workflow_path = repo_root / "workflows" / "default.yaml"
    gitignore_path = repo_root / ".gitignore"

    config_existed = config_path.exists()
    if force or not config_existed:
        _write_text(config_path, DEFAULT_CONFIG)
        actions.append(("updated" if config_existed else "created", "aio.toml"))

    workflow_existed = workflow_path.exists()
    if force or not workflow_existed:
        _write_text(workflow_path, DEFAULT_WORKFLOW)
        actions.append(("updated" if workflow_existed else "created", "workflows/default.yaml"))

    gitignore_content = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    if "# ai-orchestrator runtime artifacts" not in gitignore_content:
        content = gitignore_content.rstrip()
        if content:
            content += "\n\n"
        content += GITIGNORE_BLOCK.rstrip() + "\n"
        _write_text(gitignore_path, content)
        actions.append(("updated" if gitignore_content else "created", ".gitignore"))

    return actions


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


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_WORKFLOW",
    "GITIGNORE_BLOCK",
    "detect_shell",
    "install_shell_integration",
    "scaffold_repository",
]
