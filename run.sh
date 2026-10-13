#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# RTL-SDR Player - Linux/macOS launcher.
#
# First run: creates a virtualenv (.venv) and installs the Python deps
#            (Ubuntu 24.04+ refuses pip installs into the system Python,
#            so this is also the fix for "externally-managed-environment").
# Every run: starts the server, reachable from other machines on the LAN.
#
#   bash run.sh                 # recommended - LAN-visible, venv managed
#   bash run.sh --port 9000     # extra args go straight to app.py
#   python3 app.py              # DIY route: localhost-only, your own env
# ---------------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1

PY=python3

# --- pick a python ----------------------------------------------------------
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 not found - install it with:  sudo apt install python3 python3-venv"
  exit 1
fi

# --- virtualenv + deps (first run only) --------------------------------------
if [ ! -x .venv/bin/python ]; then
  echo "== first run: creating .venv and installing dependencies =="
  echo "   (this takes a minute; later starts are instant)"
  if ! "$PY" -m venv .venv; then
    echo
    echo "   could not create a virtualenv - on Ubuntu install the venv"
    echo "   package once, then re-run this script:"
    echo "     sudo apt install python3-venv"
    echo "     bash run.sh"
    exit 1
  fi
  if ! .venv/bin/pip install -q -r requirements.txt; then
    echo
    echo "   dependency install failed - check your network/proxy and re-run:"
    echo "     bash run.sh"
    exit 1
  fi
  echo "== dependencies installed =="
  echo
fi

# --- launch flags --------------------------------------------------------------
ARGS=""
# Headless box (no display): nothing to auto-open.
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  ARGS="$ARGS --no-browser"
fi
# Listen on all interfaces so the UI opens from other machines (server use).
# Prefer localhost-only? Start with  python3 app.py  instead.
ARGS="$ARGS --share"

exec .venv/bin/python app.py $ARGS "$@"
