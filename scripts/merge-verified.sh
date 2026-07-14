#!/usr/bin/env bash
set -euo pipefail

# merge-verified.sh [task ...]
#
# LOCAL merge authority (trust-model doc S6). Runs on the Director's TRUSTED
# machine, never on the VPS. For each pushed bare <task> branch (or the tasks
# named as args) it re-derives every gate on trusted infra and either MERGES
# into local main or HOLDS with a digested risk summary. It NEVER edits code.
#
# This is a thin launcher: all policy/decisions live in Python
# (orchestrator.local_merge). Config comes from <engine-dir>/env.local (the
# same knobs as the orchestrator: TEST_COMMANDS drives the local full-suite
# gate; CODEX_CMD/SENIOR_CMD drive the rw2 re-run + security panel).
#
# Push to GitHub after a merge is a separate, explicit Director action
# (Tier 3) - this script only integrates into LOCAL main.

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Run this FROM the target repo you are merging into; the engine's own
# location is irrelevant to which repo is integrated (spec §3 step 0).
REPO_DIR="$(pwd)"
git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "ERROR: run merge-verified.sh from inside the target repo" >&2; exit 1; }

ENV_FILE="$ENGINE_DIR/env.local"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export PYTHONPATH="$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"
PY="${PYTHON_BIN:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "Missing python3"

# The full local suite is the deterministic correctness gate. Default to the
# orchestrator's fast-commands if TEST_COMMANDS is unset.
exec "$PY" -m orchestrator.local_merge --repo "$REPO_DIR" "$@"
