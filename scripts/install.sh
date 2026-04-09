#!/usr/bin/env bash
set -euo pipefail

platform="$(uname -s)"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$platform" in
  Darwin)
    exec "$script_dir/install-macos.sh" "$@"
    ;;
  Linux)
    exec "$script_dir/install-linux.sh" "$@"
    ;;
  *)
    echo "Unsupported platform: $platform" >&2
    echo "Use scripts/install-windows.ps1 on Windows." >&2
    exit 1
    ;;
esac
