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

editable=0
if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--editable" ]]; then
  editable=1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

if (( editable )); then
  python3 -m pip install -e '.[dev]'
else
  if command -v pipx >/dev/null 2>&1; then
    pipx install --force ai-orchestrator
  else
    python3 -m pip install --user ai-orchestrator
  fi
fi

orch doctor
