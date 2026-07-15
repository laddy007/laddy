#!/usr/bin/env bash
set -euo pipefail

# kickoff.sh <task> [--new] [--skip-clarify]
#
# VPS entrypoint for the Python dev-loop orchestrator (design doc S11).
# Thin launcher ONLY - all policy/state/decisions live in Python:
#   1. clarify gate runs interactively in this terminal (SSH session),
#   2. the loop then runs DETACHED (nohup) and survives an SSH drop.
#
# Config comes from <engine-dir>/env.vps (exported here verbatim); see
# env.vps.example for the knobs (AGENT_REPO_URL, AGENT_WORK_ROOT,
# MAX_LOOPS, TEST_COMMANDS, CLAUDE_CMD, ...). The engine dir is derived
# from this script's own location, so the bundle works under any name.

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

TASK="${1:-}"
[ -n "$TASK" ] || die "Usage: kickoff.sh <task> [--new] [--skip-clarify]"
[[ "$TASK" =~ ^[a-zA-Z0-9._-]+$ ]] || die "Invalid task name: $TASK"
[ "$TASK" != "main" ] || die "task id 'main' is reserved (hub closed namespace)"
shift || true

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

LOG_DIR="${AGENT_LOG_DIR:-$HOME/agent-logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$TASK.log"

# Phase 0 (optional): --new - interactive spec authoring when no spec exists.
# Run it foreground first, then strip --new from the args passed onward.
REST=()
DO_NEW=0
for a in "$@"; do
  if [ "$a" = "--new" ]; then DO_NEW=1; else REST+=("$a"); fi
done
if [ "$DO_NEW" = "1" ]; then
  "$PY" -m orchestrator.run "$TASK" --phase new
fi

# Phase 1: clarify - interactive, blocks until the Director answered.
# ${REST[@]+...} expands to nothing when REST is empty (safe under set -u).
"$PY" -m orchestrator.run "$TASK" --phase clarify ${REST[@]+"${REST[@]}"}

# Phase 1.5: design gate - foreground for high-risk tasks; no-op otherwise.
# A rejection (non-zero) stops kickoff: the loop is NOT detached.
"$PY" -m orchestrator.run "$TASK" --phase design ${REST[@]+"${REST[@]}"} || {
  echo "[kickoff] design gate not approved; not detaching the loop." >&2
  exit 1
}

# Phase 2: loop - detached, survives SSH drop.
# -u: unbuffered, so a crash before the terminal-state print does not swallow
# buffered output (an empty $LOG that looked like an instant death otherwise).
# LADDY_LOG_HEARTBEAT: mirror each iteration-log entry to $LOG as it happens.
LADDY_LOG_HEARTBEAT=1 nohup "$PY" -u -m orchestrator.run "$TASK" --phase loop ${REST[@]+"${REST[@]}"} >> "$LOG" 2>&1 < /dev/null &
PID=$!
echo "[kickoff] loop detached (pid $PID); log: $LOG"
echo "[kickoff] follow with: tail -f $LOG"
