#!/usr/bin/env bash
set -euo pipefail

# phone.sh - launch the laddy-phone control server (see phone/server.py).
#
# Thin launcher ONLY, mirroring kickoff.sh: derive the engine dir from this
# script's own location, export env.vps verbatim if present, then exec the
# Python server in the foreground (systemd or tmux owns the lifecycle).
#
# Required config (env.vps or the environment): LADDY_PHONE_TOKEN - the
# server refuses to start without it (exit 2). Optional: LADDY_PHONE_BIND
# (default 127.0.0.1:8787 - bind your tailnet IP to reach it from a phone),
# AGENT_WORK_ROOT, AGENT_LOG_DIR, PYTHON_BIN.

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="$ENGINE_DIR/env.vps"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export PYTHONPATH="$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"
PY="${PYTHON_BIN:-python3}"

command -v "$PY" >/dev/null 2>&1 || die "Missing python3"

cd "$ENGINE_DIR"
exec "$PY" -m phone.server
