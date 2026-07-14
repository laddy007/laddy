#!/usr/bin/env bash
set -euo pipefail

# watch-vps.sh <task>
#
# Follow a task's VPS work log with color (roles blue, files yellow).
# Ctrl-C to stop watching -- the VPS job keeps running.
#
# Config (override in <engine-dir>/env.local): VPS_SSH, AGENT_LOG_DIR.

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

VPS_SSH="${VPS_SSH:-myapp-vps}"
# kickoff.sh writes to ${AGENT_LOG_DIR:-$HOME/agent-logs}/<task>.log; watch the
# SAME dir (the old ~/agent default tailed a path kickoff never writes to).
AGENT_LOG_DIR="${AGENT_LOG_DIR:-~/agent-logs}"
ENV_FILE="$ENGINE_DIR/env.local"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && source "$ENV_FILE"
VPS_SSH="${VPS_SSH:-myapp-vps}"
AGENT_LOG_DIR="${AGENT_LOG_DIR:-~/agent-logs}"

TASK="${1:-}"
[ -n "$TASK" ] || die "Usage: watch-vps.sh <task>"
[[ "$TASK" =~ ^[a-zA-Z0-9._-]+$ ]] || die "Invalid task: $TASK."

LOG="$AGENT_LOG_DIR/$TASK.log"
COLOR="$SCRIPT_DIR/colorize-log.sh"

if [ -t 1 ] && [ -x "$COLOR" ]; then
  ssh "$VPS_SSH" "tail -n +1 -f $LOG" | "$COLOR"
else
  ssh "$VPS_SSH" "tail -n +1 -f $LOG"
fi
