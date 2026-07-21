#!/usr/bin/env bash
set -euo pipefail

# local-task.sh <task> [--new] [--reseed]
#
# Run the dev-loop ENTIRELY LOCALLY (no VPS), in WSL/Linux on ext4. It:
#   1. bootstraps a local transport hub (bare) + a trusted clone, both under
#      $HOME (ext4 = fast), seeded once from the current source repo,
#   2. runs the convergence loop, which pushes <task> (bare) to the hub.
# Merge the result afterwards with:  <trusted>/<engine-dir>/scripts/merge-verified.sh <task>
#
# The hub/clone split mirrors the real VPS<->local topology, but both live on
# your one machine - so this is the "no untrusted node" local rehearsal.
#
# Config (env overrides; sane defaults for a Windows+WSL setup):
#   LOCAL_SOURCE_REPO   where the current code lives      (default /mnt/c/myapp)
#   LOCAL_BASE_BRANCH   branch to seed from / base tasks  (default fix/agent-loop-hardening)
#   LOCAL_HUB           bare transport repo (ext4)        (default $HOME/myapp-hub.git)
#   LOCAL_TRUSTED_REPO  trusted merge clone (ext4)        (default $HOME/myapp)
#   AGENT_WORK_ROOT     loop worktrees (ext4)             (default $HOME/agent-work)
#   TEST_COMMANDS       loop fast gate                    (default: "true" - docs smoke)
#   CLAUDE_CMD/CODEX_CMD/MAX_LOOPS  passed through to the orchestrator

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# The TARGET's in-repo artifact dir name (specs/, tasks/, ...) - a target-side
# concept, never derived from where the engine happens to be checked out
# (spec §3 step 0). Mirrors the orchestrator's own LADDY_TARGET_DIR default.
ARTIFACT_DIR_NAME="${LADDY_TARGET_DIR:-.laddy}"

TASK="${1:-}"; [ -n "$TASK" ] || die "usage: local-task.sh <task> [--new] [--reseed]"
[[ "$TASK" =~ ^[a-zA-Z0-9._-]+$ ]] || die "invalid task name: $TASK"
shift || true
DO_NEW=0; RESEED=0
for a in "$@"; do
  case "$a" in
    --new) DO_NEW=1 ;;
    --reseed) RESEED=1 ;;
    *) die "unknown flag: $a" ;;
  esac
done

SOURCE_REPO="${LOCAL_SOURCE_REPO:-/mnt/c/myapp}"
BASE_BRANCH="${LOCAL_BASE_BRANCH:-fix/agent-loop-hardening}"
HUB="${LOCAL_HUB:-$HOME/myapp-hub.git}"
TRUSTED="${LOCAL_TRUSTED_REPO:-$HOME/myapp}"
WORK_ROOT="${AGENT_WORK_ROOT:-$HOME/agent-work}"

command -v git >/dev/null || die "git not found"
PY="${PYTHON_BIN:-python3}"
command -v "$PY" >/dev/null || die "python3 not found (set PYTHON_BIN for a non-default interpreter)"
[ -d "$SOURCE_REPO/.git" ] || die "LOCAL_SOURCE_REPO has no .git: $SOURCE_REPO"

echo "[local] task=$TASK"
echo "[local] source=$SOURCE_REPO base=$BASE_BRANCH"
echo "[local] hub=$HUB trusted=$TRUSTED work=$WORK_ROOT"

case "$TRUSTED" in
  /mnt/*) echo "[local] WARNING: trusted clone is on /mnt (slow 9p). Prefer an ext4 path under \$HOME." ;;
esac

# 1. hub (bare transport). Seed 'main' from the source base branch ONLY when
#    the hub is first created (or on --reseed), so authored specs are not
#    clobbered on every run.
if [ ! -d "$HUB" ]; then
  echo "[local] init hub $HUB"
  git init --bare -b main "$HUB" >/dev/null
  RESEED=1
fi
if [ "$RESEED" = "1" ]; then
  echo "[local] seed hub main <- $SOURCE_REPO:$BASE_BRANCH"
  git -C "$SOURCE_REPO" push --force "$HUB" "$BASE_BRANCH:refs/heads/main"
fi

# 2. trusted clone (merge target). origin = hub.
if [ ! -d "$TRUSTED/.git" ]; then
  echo "[local] clone trusted $TRUSTED <- hub"
  git clone "$HUB" "$TRUSTED" >/dev/null
else
  git -C "$TRUSTED" fetch origin --prune >/dev/null
  git -C "$TRUSTED" checkout main >/dev/null 2>&1 || true
  git -C "$TRUSTED" merge --ff-only origin/main >/dev/null 2>&1 || true
fi

export PYTHONPATH="$ENGINE_DIR"
SPEC_REL="$ARTIFACT_DIR_NAME/specs/$TASK.md"

# 3. spec must be visible on hub main (the loop clones the hub).
if [ "$DO_NEW" = "1" ]; then
  echo "[local] authoring spec interactively (--new)"
  ( cd "$TRUSTED" && "$PY" -m orchestrator.run "$TASK" --phase new )
  git -C "$TRUSTED" add "$SPEC_REL"
  git -C "$TRUSTED" -c user.name=local -c user.email=local@myapp.local \
      commit -m "spec: $TASK" >/dev/null || true
  git -C "$TRUSTED" push origin main
else
  git -C "$HUB" cat-file -e "main:$SPEC_REL" 2>/dev/null \
    || die "spec $SPEC_REL not on hub main. Commit it to $BASE_BRANCH in $SOURCE_REPO (then --reseed), or use --new."
fi

# 4. run clarify (interactive) then loop (foreground). The loop pushes
#    <task> (bare) to the hub. The fast gate defaults to a no-op (docs smoke);
#    the REAL suite runs later in merge-verified's container gate.
export AGENT_REPO_URL="$HUB"
export AGENT_WORK_ROOT="$WORK_ROOT"
export DEFAULT_BRANCH="main"
export TEST_COMMANDS="${TEST_COMMANDS:-true}"
export CLAUDE_CMD="${CLAUDE_CMD:-claude -p --dangerously-skip-permissions}"
export CODEX_CMD="${CODEX_CMD:-codex exec --full-auto}"
export MAX_LOOPS="${MAX_LOOPS:-4}"

cd "$TRUSTED"
echo "[local] clarify gate (interactive; answer or say 'no questions')..."
"$PY" -m orchestrator.run "$TASK" --phase clarify

# Phase 1.5: design gate - foreground for high-risk tasks; no-op otherwise.
# A rejection (non-zero) stops the local-task: the loop does not run.
"$PY" -m orchestrator.run "$TASK" --phase design || {
  echo "[local] design gate not approved; not running the loop." >&2
  exit 1
}

echo "[local] loop (foreground; may take a few minutes - includes the container gate)..."
"$PY" -m orchestrator.run "$TASK" --phase loop

echo
echo "[local] done. If it converged, $TASK is on the hub."
echo "[local] merge it:  (cd $TRUSTED && $ENGINE_DIR/scripts/merge-verified.sh $TASK)"
