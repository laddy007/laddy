#!/usr/bin/env bash
set -euo pipefail

# push-hub.sh <user> - seed / keep-current a target repo's VPS hub.
#
# Run FROM inside the target repo (like merge-verified.sh); it pushes THAT
# repo's main to the user's bare hub on the VPS. Idempotent and used for both
# lifecycle moments:
#   - first time (seed):  adds the `laddy` remote, then pushes main.
#   - after every merge:  remote already exists, just pushes main so the next
#                         kickoff clones from an up-to-date base.
#
# The hub location is resolved from <engine-dir>/vps.conf LADDY_USERS (the
# same source upgrade_laddy.sh / vps-onboard.sh key off), so the caller only
# names a user - no need to remember the ssh URL. The URL is built to match
# exactly what vps-onboard.sh created ($HOME/repo_<project>/hub.git) and the
# ssh:// form env.local.example documents; the user's home is the parent of
# their engine-checkout path (vps.conf convention: engine_path sits directly
# under the home dir).
#
# An existing `laddy` remote pointing somewhere ELSE is a hard error, never a
# silent repoint - it could target another project's hub.

die() { echo "ERROR: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

USER_ARG="${1:-}"
[ -n "$USER_ARG" ] || die "usage: push-hub.sh <user>  (a user from vps.conf LADDY_USERS)"

# Push the CWD repo's main; the engine's own location is irrelevant to which
# repo is seeded (same contract as merge-verified.sh).
REPO_DIR="$(pwd)"
git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "run push-hub.sh from inside the target repo (its main is what gets pushed)"

CONF="$ENGINE_DIR/vps.conf"
[ -f "$CONF" ] || die "missing $CONF (see vps.conf.example)"
# shellcheck disable=SC1090
source "$CONF"
[ -n "${LADDY_USERS:-}" ] || die "LADDY_USERS not set in $CONF"

# Optional local-node knobs (remote name, branch). Same env.local the other
# local scripts read; absent is fine.
ENV_LOCAL="$ENGINE_DIR/env.local"
if [ -f "$ENV_LOCAL" ]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/lib/env_guard.sh"
  laddy_refuse_tracked_env "$ENGINE_DIR" "$ENV_LOCAL"
  # shellcheck disable=SC1090
  source "$ENV_LOCAL"
fi
REMOTE_NAME="${VPS_REMOTE_NAME:-laddy}"
BRANCH="${DEFAULT_BRANCH:-main}"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/laddy_users.sh"
laddy_parse_users  # -> ENTRY[user]="ssh_alias:engine_path:project"
[ -n "${ENTRY[$USER_ARG]:-}" ] || die "unknown user '$USER_ARG' (not in LADDY_USERS)"

IFS=: read -r ALIAS ENGINE_PATH PROJECT <<<"${ENTRY[$USER_ARG]}"
HOME_DIR="$(dirname "$ENGINE_PATH")"
HUB_URL="ssh://$ALIAS$HOME_DIR/repo_$PROJECT/hub.git"

# Ensure the remote exists and points where we expect - never silently repoint.
if EXISTING="$(git -C "$REPO_DIR" remote get-url "$REMOTE_NAME" 2>/dev/null)"; then
  [ "$EXISTING" = "$HUB_URL" ] \
    || die "remote '$REMOTE_NAME' already set to '$EXISTING', expected '$HUB_URL' - if intentional, reconcile by hand: git remote set-url $REMOTE_NAME '$HUB_URL'"
  echo "[push-hub] remote '$REMOTE_NAME' -> $HUB_URL (already set)"
else
  git -C "$REPO_DIR" remote add "$REMOTE_NAME" "$HUB_URL"
  echo "[push-hub] added remote '$REMOTE_NAME' -> $HUB_URL"
fi

git -C "$REPO_DIR" rev-parse --verify "$BRANCH" >/dev/null 2>&1 \
  || die "branch '$BRANCH' does not exist in $REPO_DIR"

echo "[push-hub] pushing $BRANCH -> $REMOTE_NAME"
git -C "$REPO_DIR" push "$REMOTE_NAME" "$BRANCH:$BRANCH"
echo "[push-hub] done"