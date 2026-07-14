#!/usr/bin/env bash
set -euo pipefail

# upgrade_laddy.sh [user...] - promote the local engine main into each VPS
# user's ~/laddy (spec 4.7). Runs LOCALLY. No args = ALL users from
# LADDY_USERS, all-or-nothing: if ANY user fails preflight (loop in flight,
# or a dirty engine tree that updateInstead would silently refuse), NOTHING
# is upgraded and the busy/dirty users are reported. Named users = upgrade
# exactly those (each still preflighted).

die() { echo "ERROR: $*" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONF="$ENGINE_DIR/vps.conf"
[ -f "$CONF" ] || die "missing $CONF (see vps.conf.example)"
# shellcheck disable=SC1090
source "$CONF"
[ -n "${LADDY_USERS:-}" ] || die "LADDY_USERS not set in $CONF"

# ---- resolve the target set -------------------------------------------------
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/laddy_users.sh"
laddy_parse_users  # -> ENTRY[user]="ssh_alias:engine_path:project", ORDER=(user...)
# upgrade_laddy.sh only pushes the engine checkout; it ignores the project
# field (4th) that vps-onboard.sh needs for the hub path.
if [ "$#" -gt 0 ]; then
  USERS=("$@")
  for u in "${USERS[@]}"; do
    [ -n "${ENTRY[$u]:-}" ] || die "unknown user '$u' (not in LADDY_USERS)"
  done
else
  USERS=("${ORDER[@]}")
fi

# ---- preflight: ALL users first, upgrade only if every one is clear ---------
UPGRADABLE=() BUSY=()
for u in "${USERS[@]}"; do
  IFS=: read -r alias path _project <<<"${ENTRY[$u]}"
  status=$(ssh "$alias" "
    if pgrep -u \"\$(id -un)\" -f 'orchestrator.run' >/dev/null 2>&1; then echo busy-loop;
    elif [ -n \"\$(git -C '$path' status --porcelain 2>/dev/null)\" ]; then echo dirty-tree;
    else echo clear; fi" 2>/dev/null) || status=unreachable
  if [ "$status" = clear ]; then UPGRADABLE+=("$u"); else BUSY+=("$u($status)"); fi
done

if [ "${#BUSY[@]}" -gt 0 ]; then
  echo "upgradable now: ${UPGRADABLE[*]:-<none>}"
  echo "busy/dirty:     ${BUSY[*]}"
  die "preflight failed - NOTHING upgraded (all-or-nothing; name users explicitly to upgrade a subset)"
fi

# ---- promote ----------------------------------------------------------------
for u in "${USERS[@]}"; do
  IFS=: read -r alias path _project <<<"${ENTRY[$u]}"
  echo "[upgrade] $u <- local main"
  git -C "$ENGINE_DIR" push "ssh://$alias$path" main:main
done
echo "[upgrade] done: ${USERS[*]}"
