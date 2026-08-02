#!/usr/bin/env bash
#
# Create a self-contained Python virtual environment for Simple-Pluto-RADAR.
#
# The venv lives in .venv/ next to this script, so the whole project is
# portable: clone anywhere, run this, activate, go. Nothing is installed
# outside the repo except the native libiio library, which pip cannot
# provide (see the check at the end of this script).
#
# Usage:
#   ./setup_env.sh                        # create/update .venv
#   ./setup_env.sh --recreate             # delete .venv and start clean
#   ./setup_env.sh --python python3.11    # pick a specific interpreter
#   ./setup_env.sh --system-site-packages # reuse an apt-installed python3-libiio

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
REQUIREMENTS="$REPO_ROOT/requirements.txt"

PYTHON="${PYTHON:-}"
PYTHON_EXPLICIT=0
RECREATE=0
VENV_FLAGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --python)
            PYTHON="${2:-}"
            [ -n "$PYTHON" ] || { echo "error: --python needs an argument" >&2; exit 2; }
            PYTHON_EXPLICIT=1
            shift 2
            ;;
        --recreate)
            RECREATE=1
            shift
            ;;
        --system-site-packages)
            VENV_FLAGS+=("--system-site-packages")
            shift
            ;;
        -h|--help)
            awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "error: unknown option '$1' (try --help)" >&2
            exit 2
            ;;
    esac
done

if [ ! -f "$REQUIREMENTS" ]; then
    echo "error: $REQUIREMENTS not found. Run this script from a full checkout." >&2
    exit 1
fi

# --- Pick an interpreter ---------------------------------------------------

if [ -z "$PYTHON" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi

if [ -z "$PYTHON" ] || ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: no Python interpreter found. Install Python 3.8+ or pass --python PATH." >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "error: $("$PYTHON" -V 2>&1) is too old. pyadi-iio needs Python 3.8+." >&2
    exit 1
fi

echo "==> Using $("$PYTHON" -V 2>&1) at $(command -v "$PYTHON")"

# --- Create the venv -------------------------------------------------------

if [ "$RECREATE" -eq 1 ] && [ -d "$VENV_DIR" ]; then
    echo "==> Removing existing $VENV_DIR"
    rm -rf "$VENV_DIR"
fi

if [ -d "$VENV_DIR" ]; then
    # An existing venv keeps whatever interpreter and site-packages policy it
    # was built with, so flags that only apply at creation time are a no-op
    # here. Say so rather than pretending they took effect.
    if [ "$PYTHON_EXPLICIT" -eq 1 ]; then
        echo "    note: --python only applies when creating a venv; reusing the existing one." >&2
        echo "          Pass --recreate to rebuild with $PYTHON." >&2
    fi
    if [ ${#VENV_FLAGS[@]} -gt 0 ]; then
        echo "    note: --system-site-packages only applies when creating a venv." >&2
        echo "          Pass --recreate to rebuild with it." >&2
    fi

    # A venv whose interpreter symlink is dangling (system Python upgraded or
    # removed out from under it) looks fine as a directory but is unusable.
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        echo "error: $VENV_DIR exists but has no working interpreter." >&2
        echo "       This usually means the Python it was built against is gone." >&2
        echo "       Rebuild it with: $0 --recreate" >&2
        exit 1
    fi

    echo "==> Reusing existing venv at $VENV_DIR"
else
    echo "==> Creating venv at $VENV_DIR"
    if ! "$PYTHON" -m venv "${VENV_FLAGS[@]+"${VENV_FLAGS[@]}"}" "$VENV_DIR"; then
        cat >&2 <<'EOF'

error: could not create the virtual environment.

On Debian/Ubuntu the venv module ships separately:
    sudo apt install python3-venv
EOF
        exit 1
    fi
fi

VENV_PY="$VENV_DIR/bin/python"

# --- Install dependencies --------------------------------------------------

echo "==> Upgrading pip"
"$VENV_PY" -m pip install --upgrade pip --quiet

echo "==> Installing dependencies from requirements.txt"
"$VENV_PY" -m pip install -r "$REQUIREMENTS"

# --- Check for the native libiio library -----------------------------------
#
# pyadi-iio depends on pylibiio, which is a pure-Python ctypes wrapper. It
# looks up the real library with ctypes.util.find_library("iio"). If that
# returns nothing, "import adi" fails no matter what pip installed.

echo "==> Checking for native libiio"
if "$VENV_PY" - <<'EOF'
import sys
from ctypes.util import find_library

found = find_library("iio")
if found:
    print("    found libiio: %s" % found)
    sys.exit(0)
sys.exit(1)
EOF
then
    if "$VENV_PY" -c 'import adi' 2>/dev/null; then
        echo "    import adi: OK"
    else
        echo "    warning: libiio was found but 'import adi' still failed." >&2
        echo "    Run '$VENV_PY -c \"import adi\"' to see the error." >&2
    fi
else
    cat >&2 <<'EOF'

    warning: native libiio not found. The venv is fine, but "import adi"
    will fail until you install libiio at the OS level:

      Debian/Ubuntu   sudo apt install libiio0 libiio-utils
      Fedora          sudo dnf install libiio libiio-utils
      Arch            sudo pacman -S libiio
      macOS           brew install libiio
      Windows         use the Analog Devices libiio installer

    Verify with:  iio_info -u ip:pluto.local
EOF
fi

# --- Done ------------------------------------------------------------------

cat <<EOF

Done. Activate the environment with:

    source $VENV_DIR/bin/activate

Then run the radar with:

    python main.py

EOF
