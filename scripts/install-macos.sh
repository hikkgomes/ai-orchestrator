#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/install-macos.sh [--editable]

Options:
  --editable   Install the current checkout in editable development mode.

Default behavior installs ai-orchestrator with pipx when available.
EOF
}

EDITABLE=false
if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--editable" ]]; then
  EDITABLE=true
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

if [[ "$EDITABLE" == "true" ]]; then
  python3 -m pip install -e '.[dev]'
else
  if command -v pipx >/dev/null 2>&1; then
    pipx install --force .
  else
    python3 -m pip install --user .
  fi
fi

# Write install metadata so `orch update` knows where to pull from.
INSTALL_META_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ai-orchestrator"
mkdir -p "$INSTALL_META_DIR"
SOURCE_PATH="$(pwd)"
if [[ "$EDITABLE" == "true" ]]; then
  MODE="editable"
elif command -v pipx >/dev/null 2>&1; then
  MODE="local-pipx"
else
  MODE="pip-user"
fi
SOURCE_REPO_PATH="$SOURCE_PATH" INSTALL_MODE="$MODE" INSTALL_META_DIR="$INSTALL_META_DIR" \
  python3 -c "
import json, os, pathlib
d = pathlib.Path(os.environ['INSTALL_META_DIR'])
d.mkdir(parents=True, exist_ok=True)
(d / 'install-meta.json').write_text(
    json.dumps(
        {
            'source_repo_path': os.environ['SOURCE_REPO_PATH'],
            'install_mode': os.environ['INSTALL_MODE'],
        }
    ),
    encoding='utf-8',
)
"

orch doctor
