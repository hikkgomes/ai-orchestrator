"""Centralized runtime path resolution for ai-orchestrator artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path


APP_NAME = "ai-orchestrator"


def get_user_data_dir() -> Path:
    """Return the platform-specific base data directory for ai-orchestrator."""
    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def get_projects_root() -> Path:
    return get_user_data_dir() / "projects"


def get_project_dir(repo_root: Path) -> Path:
    """Return a deterministic per-repository artifact directory."""
    canonical = repo_root.resolve()
    slug = _slugify_repo_name(canonical.name)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:8]
    return get_projects_root() / f"{slug}-{digest}"


def get_project_config_path(repo_root: Path) -> Path:
    return get_project_dir(repo_root) / "config.toml"


def get_project_workflow_path(repo_root: Path) -> Path:
    return get_project_dir(repo_root) / "workflow.yaml"


def get_project_review_dir(repo_root: Path) -> Path:
    return get_project_dir(repo_root) / "review"


def get_worktrees_dir(repo_root: Path) -> Path:
    return get_project_dir(repo_root) / "worktrees"


def get_state_dir(repo_root: Path) -> Path:
    return get_project_dir(repo_root) / "state"


def get_global_metadata_db() -> Path:
    return get_user_data_dir() / "metadata.sqlite3"


def _slugify_repo_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-").lower()
    return slug or "repo"
