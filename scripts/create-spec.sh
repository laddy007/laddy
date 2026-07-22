#!/usr/bin/env bash
set -euo pipefail

# create-spec.sh <task>
#
# DIRECTOR-SIDE, LOCAL spec authoring. Runs ONLY the interactive spec-authoring
# phase (orchestrator.run --phase new) on the Director's trusted machine, using
# <engine-dir>/env.local (never env.vps / vps.conf, never the VPS). It stops
# after the spec is authored, committed, and pushed to the hub; the Director
# then runs the task on the VPS with `kickoff <task>` (no --new).
#
# This fills the "author a spec here, run it there" gap: today --phase new is
# only reachable via kickoff.sh --new (VPS-only, needs env.vps) or
# local-task.sh --new (which then runs the whole loop locally). Thin launcher:
# all authoring/commit/push policy lives in Python (_phase_new); this script
# only wires env + guards and forwards to that one phase. The engine dir is
# derived from this script's own location, so the bundle works under any name.

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Validate the task id BEFORE any env sourcing - a rejected name creates no
# worktree, sources nothing, and reaches no python. Same three checks as
# kickoff.sh: non-empty, safe charset, and the reserved 'main'.
TASK="${1:-}"
[ -n "$TASK" ] || die "Usage: create-spec.sh <task>"
[[ "$TASK" =~ ^[a-zA-Z0-9._-]+$ ]] || die "Invalid task name: $TASK"
[ "$TASK" != "main" ] || die "task id 'main' is reserved (hub closed namespace)"

# env.local is REQUIRED on this (local, trusted) node - unlike kickoff's
# source-if-present env.vps. A missing one is a hard error, never a silent
# fall back to orchestrator defaults.
ENV_FILE="$ENGINE_DIR/env.local"
[ -f "$ENV_FILE" ] || die "missing $ENV_FILE - run ./scripts/local-onboard.sh for this project, or copy env.local.example to env.local and edit it."

# env.local is 'set -a; source'd and so runs as the Director; refuse a
# git-TRACKED one (an injection vector - the legitimate file is always
# gitignored/untracked). Quiet on the normal untracked path.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/env_guard.sh"
laddy_refuse_tracked_env "$ENGINE_DIR" "$ENV_FILE"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export PYTHONPATH="$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"
PY="${PYTHON_BIN:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "Missing python3 (set PYTHON_BIN for a non-default interpreter)"

# The ONLY phase this launcher runs: interactive spec authoring, foreground
# (the co-authoring TUI must reach the Director's terminal). No clarify/design/
# loop here - the task runs later on the VPS. A non-zero exit (spec already
# exists, or authoring added nothing beyond the headline) aborts under set -e
# and propagates, so the success hint below prints ONLY when authoring stuck.
"$PY" -m orchestrator.run "$TASK" --phase new

echo
echo "[create-spec] spec for '$TASK' authored and pushed to the hub."
echo "[create-spec] run it on the VPS with:  kickoff $TASK   (no --new)"
