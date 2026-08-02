#!/usr/bin/env bash
#
# One-shot dependency setup for Simple-Pluto-RADAR on Debian/Ubuntu.
#
# Two things are needed to run this project:
#   1. native libiio, which pip cannot install (apt handles it)
#   2. the Python venv, which setup_env.sh builds
#
# This script does both. On other platforms, install libiio yourself (see
# docs/usage.md) and run ./setup_env.sh directly.
#
# Any arguments are passed through to setup_env.sh, so this works:
#   ./setup-dependencies.sh --recreate

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v apt-get >/dev/null 2>&1; then
    echo "error: no apt-get found -- this script is Debian/Ubuntu only." >&2
    echo "       Install libiio with your package manager, then run:" >&2
    echo "           ./setup_env.sh" >&2
    echo "       See docs/usage.md for per-OS instructions." >&2
    exit 1
fi

echo "==> Installing native libiio (requires sudo)"
if ! sudo apt-get install -y libiio0 libiio-utils; then
    echo >&2
    echo "error: apt could not install libiio. If the package index is stale, try:" >&2
    echo "           sudo apt-get update" >&2
    exit 1
fi

echo "==> Building the Python virtual environment"
# Executed, not sourced. setup_env.sh calls exit on its error paths, which
# would terminate this script silently if it were sourced -- and sourcing
# would not activate the venv anyway, since setup_env.sh only creates it.
"$REPO_ROOT/setup_env.sh" "$@"
