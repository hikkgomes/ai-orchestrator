"""Install and refresh repository-local reviewer configuration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib.resources
import json
from pathlib import Path
from typing import Any

from . import load_config
from .detect_architecture import detect_architecture
from .detect_commands import detect_commands


DEFAULT_IGNORE = [
    ".git/",
    "node_modules/",
    ".next/",
    "dist/",
    "build/",
    "coverage/",
    ".turbo/",
    ".vercel/",
    ".cache/",
    "vendor/",
    "target/",
]


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "project": {
            "stack": [],
            "package_managers": [],
            "monorepo": False,
        },
        "commands": {
            "install": "",
            "lint": "",
            "typecheck": "",
            "test": "",
            "build": "",
            "format": "",
        },
        "paths": {
            "generated": [],
            "ignore": list(DEFAULT_IGNORE),
            "critical": [],
        },
        "risk": {
            "auth_sensitive": [],
            "payment_sensitive": [],
            "migration_sensitive": [],
            "infra_sensitive": [],
            "pii_sensitive": [],
            "destructive_sensitive": [],
        },
        "architecture": {
            "patterns": [],
            "key_libraries": {
                "framework": [],
                "database": [],
                "testing": [],
                "http": [],
                "state_management": [],
                "styling": [],
                "auth": [],
                "payments": [],
                "validation": [],
            },
            "naming": {
                "files": "",
                "variables": "",
                "components": "",
                "tests": "",
            },
            "folder_structure": {},
            "config_files": [],
            "entry_points": [],
            "project_description": "",
            "env_keys": [],
            "docker_services": [],
            "ai_refinement_needed": [],
        },
        "workspaces": {},
        "notes": [],
        "uncertain": [],
        "analyzed_at": "",
    }


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _bundled_rules_text() -> str:
    package = importlib.resources.files("ai_orchestrator.reviewer")
    return (package / "rules.yaml").read_text(encoding="utf-8")


def _detected_config(root: Path) -> dict[str, Any]:
    config = _default_config()
    command_data = detect_commands(root)
    architecture_data = detect_architecture(root)

    config["project"] = command_data["project"]
    config["commands"] = command_data["commands"]
    config["paths"]["generated"] = command_data["paths"]["generated"]
    config["paths"]["ignore"] = _dedupe(config["paths"]["ignore"] + command_data["paths"]["ignore"])
    config["paths"]["critical"] = command_data["paths"]["critical"]
    config["risk"] = command_data["risk"]
    config["workspaces"] = command_data["workspaces"]
    config["notes"] = command_data["notes"]
    config["uncertain"] = command_data["uncertain"]
    config["architecture"] = architecture_data["architecture"]
    config["analyzed_at"] = _timestamp()
    return config


def _merge_for_reanalysis(existing: dict[str, Any], detected: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(_default_config())
    merged.update({key: value for key, value in existing.items() if key not in {"project", "commands", "paths", "risk", "architecture", "workspaces", "notes", "uncertain", "analyzed_at"}})
    merged["project"] = detected["project"]
    merged["commands"] = detected["commands"]
    merged["workspaces"] = detected["workspaces"]
    merged["architecture"] = detected["architecture"]
    merged["paths"]["generated"] = _dedupe(detected["paths"]["generated"] + ((existing.get("paths") or {}).get("generated") or []))
    merged["paths"]["ignore"] = _dedupe(detected["paths"]["ignore"] + ((existing.get("paths") or {}).get("ignore") or []))
    merged["paths"]["critical"] = _dedupe(((existing.get("paths") or {}).get("critical") or []) + detected["paths"]["critical"])

    existing_risk = existing.get("risk") or {}
    for key, detected_values in detected["risk"].items():
        merged["risk"][key] = _dedupe((existing_risk.get(key) or []) + detected_values)

    merged["notes"] = _dedupe((existing.get("notes") or []) + detected["notes"])
    merged["uncertain"] = _dedupe(detected["uncertain"] + (existing.get("uncertain") or []))
    merged["analyzed_at"] = _timestamp()
    return merged


def _write_config(root: Path, config: dict[str, Any]) -> Path:
    path = root / ".ai-review" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _write_rules(root: Path) -> Path:
    path = root / ".ai-review" / "rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_bundled_rules_text(), encoding="utf-8")
    return path


def _summary(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "stack": config["project"]["stack"],
        "package_managers": config["project"]["package_managers"],
        "monorepo": config["project"]["monorepo"],
        "workspaces": sorted(config["workspaces"].keys()),
        "commands": {key: value for key, value in config["commands"].items() if value},
        "critical_paths": config["paths"]["critical"],
        "risk": {key: value for key, value in config["risk"].items() if value},
        "architecture_patterns": config["architecture"]["patterns"],
        "manual_refinement_needed": config["architecture"]["ai_refinement_needed"],
    }


def install_reviewer(root: Path) -> dict[str, Any]:
    """First-time setup for ``.ai-review/config.json`` and ``rules.yaml``."""

    root = root.resolve()
    config = _detected_config(root)
    config_path = _write_config(root, config)
    rules_path = _write_rules(root)
    return {
        "action": "installed",
        "config_path": config_path,
        "rules_path": rules_path,
        "config": config,
        "summary": _summary(config),
    }


def analyze_repo(root: Path) -> dict[str, Any]:
    """Refresh auto-detected reviewer settings while preserving curated fields."""

    root = root.resolve()
    existing = load_config(root) or _default_config()
    detected = _detected_config(root)
    merged = _merge_for_reanalysis(existing, detected)
    config_path = _write_config(root, merged)
    rules_path = _write_rules(root)
    return {
        "action": "analyzed",
        "config_path": config_path,
        "rules_path": rules_path,
        "config": merged,
        "summary": _summary(merged),
    }
