#!/usr/bin/env bash
set -euo pipefail

# local-onboard.sh
#
# One-shot, idempotent, interactive onboarding for the TRUSTED/local side of
# a target project (mirror of vps-onboard.sh, which does the VPS side). Run
# this on your own machine, once per target project. It never touches the
# VPS as root and never clones from GitHub - your project checkout must
# already exist (git clone it yourself first); this script only wires it up
# to talk to its hub.
#
# Loops over every entry in LADDY_TARGETS (see local.conf.example - schema
# and parsing shared with scripts/lib/laddy_targets.sh). For each project:
#   - verifies local_repo_path exists and is a git repo
#   - adds/updates the `laddy` git remote there, pointing at
#     ssh://<ssh_alias><hub_path>
#   - verifies `git fetch laddy` succeeds
#   - writes local_repo_path/env.local from env.local.example with
#     AGENT_REPO_URL/AGENT_BRANCH_REMOTE pre-filled (leaves
#     SETUP_COMMANDS/TEST_COMMANDS/CODEX_CMD as template defaults -
#     project-specific, edit by hand)
#   - warns (does not fail) if the claude/codex CLIs are missing - the
#     cross-vendor rw2 panel degrades without codex
#
# Config: <engine-dir>/local.conf (git-ignored, like vps.conf/env.local).
# Missing values are prompted for once and saved, so re-runs are
# non-interactive. Every step is idempotent (checks before creating) so
# re-running after a partial failure is safe.

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[onboard] $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONF="$ENGINE_DIR/local.conf"

command -v ssh >/dev/null 2>&1 || die "ssh not found"
command -v git >/dev/null 2>&1 || die "git not found"

# --------------------------------------------------------------- config ---

ask() {
  local prompt="$1" default="${2:-}" ans
  read -r -p "$prompt${default:+ [$default]}: " ans
  echo "${ans:-$default}"
}

# Guards every value that lands in $CONF: this file is `source`d (here, and
# again on every future run) - an unescaped `"` in an answer would close the
# quoted assignment early and hand the rest of the line to the shell as live
# commands the NEXT time $CONF is sourced (second-order injection into a
# trusted-machine shell). Reject at capture time so nothing unsafe ever
# reaches the heredoc; matches the per-field charsets laddy_targets.sh
# already enforces on read, so a hand-edited $CONF still gets checked too.
_safe_token() {
  [[ "$2" =~ ^[a-zA-Z0-9._-]+$ ]] || die "$1 has unexpected characters: $2"
}
_safe_path() {
  [[ "$2" == /* && "$2" =~ ^[a-zA-Z0-9._/-]+$ ]] || die "$1 must be an absolute path with only [a-zA-Z0-9._/-]: $2"
}

if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  set -a; source "$CONF"; set +a
  info "using existing config: $CONF (edit LADDY_TARGETS by hand to add projects, then re-run)"
else
  info "no $CONF yet - answer a few questions once, saved for future re-runs"
  T1_PROJECT=$(ask "target project name" "laddy")
  _safe_token T1_PROJECT "$T1_PROJECT"
  T1_ALIAS=$(ask "ssh config host alias for the VPS user who owns this project's hub" "vps-$T1_PROJECT")
  _safe_token T1_ALIAS "$T1_ALIAS"
  T1_HUB=$(ask "absolute hub path on the VPS (as seen by that alias)" "/home/$T1_PROJECT/repo_$T1_PROJECT/hub.git")
  _safe_path T1_HUB "$T1_HUB"
  T1_PATH=$(ask "absolute path to this project's trusted checkout on THIS machine" "$ENGINE_DIR")
  _safe_path T1_PATH "$T1_PATH"
  LADDY_TARGETS="$T1_PROJECT:$T1_ALIAS:$T1_HUB:$T1_PATH"
  cat > "$CONF" <<EOF
# Written by local-onboard.sh - edit freely; re-run the script to apply changes.
# Git-ignored (see .gitignore) - machine-local (hostnames, paths).
# See local.conf.example for the LADDY_TARGETS schema; to onboard more than
# one project, add more space-separated
# project:ssh_alias:hub_path:local_repo_path entries by hand and re-run.
LADDY_TARGETS="$LADDY_TARGETS"
EOF
  info "saved $CONF"
fi

[ -n "${LADDY_TARGETS:-}" ] || die "$CONF missing LADDY_TARGETS (delete the file and re-run to be asked again)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/laddy_targets.sh"
laddy_parse_targets  # -> ENTRY[project]="ssh_alias:hub_path:local_repo_path", ORDER=(project...)

info "onboarding targets: ${ORDER[*]}"

for p in "${ORDER[@]}"; do
  IFS=: read -r alias hub path <<<"${ENTRY[$p]}"

  info "[$p] checking local checkout: $path"
  [ -d "$path/.git" ] || die "[$p] $path is not a git repo - clone it from GitHub yourself first, this script never clones"

  info "[$p] checking ssh connectivity: $alias"
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$alias" true 2>/dev/null \
    || die "[$p] ssh $alias failed - check ~/.ssh/config Host $alias and that your key is in that VPS user's authorized_keys"

  REMOTE_URL="ssh://$alias$hub"
  EXISTING="$(git -C "$path" remote get-url laddy 2>/dev/null || true)"
  if [ "$EXISTING" = "$REMOTE_URL" ]; then
    info "[$p] remote 'laddy' already set: $REMOTE_URL"
  elif [ -n "$EXISTING" ]; then
    git -C "$path" remote set-url laddy "$REMOTE_URL"
    info "[$p] updated remote 'laddy': $EXISTING -> $REMOTE_URL"
  else
    git -C "$path" remote add laddy "$REMOTE_URL"
    info "[$p] added remote 'laddy': $REMOTE_URL"
  fi

  info "[$p] verifying fetch"
  git -C "$path" fetch laddy --prune --quiet \
    || die "[$p] git fetch laddy failed against $REMOTE_URL"

  ENV_FILE="$path/env.local"
  if [ -f "$ENV_FILE" ]; then
    info "[$p] $ENV_FILE already exists - leaving it alone (edit by hand if needed)"
  else
    sed -e "s|^AGENT_REPO_URL=.*|AGENT_REPO_URL=$REMOTE_URL|" \
        -e "s|^AGENT_BRANCH_REMOTE=.*|AGENT_BRANCH_REMOTE=laddy|" \
        "$ENGINE_DIR/env.local.example" > "$ENV_FILE"
    info "[$p] wrote $ENV_FILE from env.local.example (AGENT_REPO_URL/AGENT_BRANCH_REMOTE filled in - review SETUP_COMMANDS/TEST_COMMANDS/CODEX_CMD before running merge-verified.sh)"
  fi

  for cli in claude codex; do
    if ! command -v "$cli" >/dev/null 2>&1; then
      if [ "$cli" = codex ]; then
        echo "[onboard] [$p] NOTE: codex CLI not found - the cross-vendor rw2 panel degrades without it" >&2
      else
        echo "[onboard] [$p] WARNING: claude CLI not found - install + 'claude login' manually" >&2
      fi
    fi
  done

  info "[$p] done"
done

echo
echo "[onboard] done: ${ORDER[*]}"
echo
echo "Next: cd into a target's local_repo_path and run:"
echo "  ./scripts/merge-verified.sh <task-branch>"
