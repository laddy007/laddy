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
# location is irrelevant to which repo is integrated (spec section 3 step 0).
REPO_DIR="$(pwd)"
git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "ERROR: run merge-verified.sh from inside the target repo" >&2; exit 1; }

# env.local: the TARGET's own file wins (local-onboard.sh writes it into the
# target checkout); the engine's is the fallback (engine-as-target / legacy).
# Before this, the generated per-target env.local was never read at all.
ENV_FILE="$REPO_DIR/env.local"
ENV_REPO="$REPO_DIR"
if [ ! -f "$ENV_FILE" ]; then
  ENV_FILE="$ENGINE_DIR/env.local"
  ENV_REPO="$ENGINE_DIR"
fi
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/lib/env_guard.sh"
  laddy_refuse_tracked_env "$ENV_REPO" "$ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "WARN: $ENV_FILE not found - falling back to orchestrator defaults (fast-commands, no TEST_COMMANDS gate)." >&2
  echo "      Run ./scripts/local-onboard.sh to generate it for this project (see local.conf.example)." >&2
fi

# --local judges a locally-committed revision with no VPS round trip, so it must
# run with no hub configured at all. Detect it in the forwarded args to soften
# the remote-required check below.
LOCAL_MODE=0
for arg in "$@"; do
  if [ "$arg" = "--local" ]; then LOCAL_MODE=1; break; fi
done

# discover_ready fetches task branches from the AGENT_BRANCH_REMOTE remote
# (default origin) - fail fast with the exact fix instead of a raw git error
# if that remote isn't wired up yet. Under --local the remote is optional (the
# mode is "no VPS"), so a missing one is a warning, not a die.
BRANCH_REMOTE="${AGENT_BRANCH_REMOTE:-origin}"
if ! git -C "$REPO_DIR" remote get-url "$BRANCH_REMOTE" >/dev/null 2>&1; then
  if [ "$LOCAL_MODE" -eq 1 ]; then
    echo "WARN: git remote '$BRANCH_REMOTE' (AGENT_BRANCH_REMOTE) not found in $REPO_DIR - --local proceeds with no hub." >&2
  else
    die "git remote '$BRANCH_REMOTE' (AGENT_BRANCH_REMOTE) not found in $REPO_DIR - run ./scripts/local-onboard.sh to add it, or add it by hand: git -C $REPO_DIR remote add $BRANCH_REMOTE ssh://<alias><hub-path>"
  fi
fi

export PYTHONPATH="$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"
PY="${PYTHON_BIN:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "Missing python3"

# The full local suite is the deterministic correctness gate. Default to the
# orchestrator's fast-commands if TEST_COMMANDS is unset.
exec "$PY" -m orchestrator.local_merge --repo "$REPO_DIR" "$@"
