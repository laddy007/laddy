#!/usr/bin/env bash
set -euo pipefail

# vps-onboard.sh
#
# One-shot, idempotent, interactive onboarding for a fresh VPS running the
# agent dev-loop under the GitHub-free / bare-hub model (spec 2026-07-13):
# the VPS never holds a GitHub credential of ANY kind - no deploy key, no
# `gh`, no clone of a github.com remote. Every task branch reaches the VPS
# by the Director pushing from their own machine to a LOCAL bare hub that
# lives on the VPS (spec 3, 7). Run this LOCALLY (your machine, not the
# VPS): it drives the VPS over SSH as root for the one-time bootstrap, then
# per-user over each user's own SSH alias.
#
# Loops over every entry in LADDY_USERS (see vps.conf.example - schema and
# parsing shared with upgrade_laddy.sh via scripts/lib/laddy_users.sh so the
# two scripts can never drift). For each user:
#   phase 1 (root):  unix user, docker group, cgroup slice (CPUQuota/MemoryMax),
#                     local push pubkey appended to the user's authorized_keys
#                     (must happen as root - the user has no login path yet)
#   phase 2 (user):  toolchain check, bare hub (~/repo_<project>/hub.git),
#                     empty engine checkout (updateInstead), env.vps written
#
# Does NOT: register anything on GitHub, generate a deploy key, clone
# github.com, or seed the engine/hub content - that's the Director's job
# afterwards (scripts/upgrade_laddy.sh + `git push laddy main`, printed at
# the end of this script).
#
# Config: <engine-dir>/vps.conf (git-ignored, like env.local/env.vps). Missing
# values are prompted for once and saved, so re-runs are non-interactive.
# Every remote step is idempotent (checks before creating) so re-running
# after a partial failure is safe.
#
# NOTE: written and syntax-checked (bash -n) but not exercised against a
# real VPS - review before trusting it against production infra.

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[onboard] $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONF="$ENGINE_DIR/vps.conf"

command -v ssh >/dev/null 2>&1 || die "ssh not found"

_safe_token() {
  # Guards values that get interpolated into a remote `-c "... '$v' ..."`
  # string - restrict to characters that cannot break out of the quoting.
  [[ "$2" =~ ^[a-zA-Z0-9._@:/-]+$ ]] || die "$1 has unexpected characters: $2"
}

# Guards every value that lands in $CONF: this file is `source`d (here, and
# again on every future run) - an unescaped `"` (or, for the unquoted
# VAR=$VAR lines below, any shell metacharacter at all) in an answer would
# hand the rest of the line to the shell as live commands the NEXT time
# $CONF is sourced (second-order injection into a trusted-machine/root
# shell). Reject at capture time so nothing unsafe ever reaches the heredoc.
# Narrower than _safe_token (no @ or :) - these fields are LADDY_USERS'
# colon-separated columns, and must match laddy_users.sh's own per-field
# regex so a hand-edited $CONF still gets checked the same way on read.
_safe_field() {
  [[ "$2" =~ ^[a-zA-Z0-9._-]+$ ]] || die "$1 has unexpected characters: $2"
}
_safe_path() {
  [[ "$2" == /* && "$2" =~ ^[a-zA-Z0-9._/-]+$ ]] || die "$1 must be an absolute path with only [a-zA-Z0-9._/-]: $2"
}

# --------------------------------------------------------------- config ---

ask() {
  local prompt="$1" default="${2:-}" ans
  read -r -p "$prompt${default:+ [$default]}: " ans
  echo "${ans:-$default}"
}

if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  set -a; source "$CONF"; set +a
  info "using existing config: $CONF (delete it to be asked again)"
else
  info "no $CONF yet - answer a few questions once, saved for future re-runs"
  VPS_ROOT_SSH=$(ask "SSH target with root access (user@host, or an ssh-config alias)")
  _safe_token VPS_ROOT_SSH "$VPS_ROOT_SSH"
  U1=$(ask "unprivileged system user for the loop" "laddy")
  _safe_field U1 "$U1"
  U1_ALIAS=$(ask "ssh config host alias for that user (a ~/.ssh/config Host entry)" "vps-$U1")
  _safe_field U1_ALIAS "$U1_ALIAS"
  U1_PATH=$(ask "absolute engine checkout path on the VPS for that user" "/home/$U1/laddy")
  _safe_path U1_PATH "$U1_PATH"
  U1_PROJECT=$(ask "target project name for that user (hub = ~/repo_<project>/hub.git)" "myapp")
  _safe_field U1_PROJECT "$U1_PROJECT"
  LADDY_USERS="$U1:$U1_ALIAS:$U1_PATH:$U1_PROJECT"
  NTFY_TOPIC=$(ask "ntfy topic (blank = skip phone notifications for now)" "")
  [ -z "$NTFY_TOPIC" ] || _safe_field NTFY_TOPIC "$NTFY_TOPIC"
  CPU_QUOTA=$(ask "cgroup CPUQuota for each user's slice" "300%")
  [[ "$CPU_QUOTA" =~ ^[0-9]+%$ ]] || die "CPU_QUOTA must look like 300% (got: $CPU_QUOTA)"
  MEMORY_MAX=$(ask "cgroup MemoryMax for each user's slice" "8G")
  [[ "$MEMORY_MAX" =~ ^[0-9]+[KMG]$ ]] || die "MEMORY_MAX must look like 8G (got: $MEMORY_MAX)"
  cat > "$CONF" <<EOF
# Written by vps-onboard.sh - edit freely; re-run the script to apply changes.
# Git-ignored (see .gitignore) - machine-local (hostnames, paths).
# See vps.conf.example for the LADDY_USERS schema; to onboard more than one
# user, add more space-separated user:ssh_alias:engine_path:project entries
# by hand and re-run this script.
VPS_ROOT_SSH=$VPS_ROOT_SSH
LADDY_USERS="$LADDY_USERS"
NTFY_TOPIC=$NTFY_TOPIC
CPU_QUOTA=$CPU_QUOTA
MEMORY_MAX=$MEMORY_MAX
EOF
  info "saved $CONF"
fi

for v in VPS_ROOT_SSH LADDY_USERS CPU_QUOTA MEMORY_MAX; do
  [ -n "${!v:-}" ] || die "$CONF missing $v (delete the file and re-run to be asked again)"
done
_safe_token VPS_ROOT_SSH "$VPS_ROOT_SSH"
[[ "$CPU_QUOTA" =~ ^[0-9]+%$ ]] || die "CPU_QUOTA must look like 300% (got: $CPU_QUOTA)"
[[ "$MEMORY_MAX" =~ ^[0-9]+[KMG]$ ]] || die "MEMORY_MAX must look like 8G (got: $MEMORY_MAX)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/laddy_users.sh"
laddy_parse_users  # -> ENTRY[user]="ssh_alias:engine_path:project", ORDER=(user...)

R() { ssh -o BatchMode=yes "$VPS_ROOT_SSH" "$@"; }

# ------------------------------------------------------------ pubkey ---

PUBKEY_PATH=$(ask "LOCAL pubkey to grant each VPS user push access (your machine's key, NOT a new one)" "$HOME/.ssh/id_ed25519.pub")
[ -f "$PUBKEY_PATH" ] || die "pubkey not found: $PUBKEY_PATH (generate one first: ssh-keygen -t ed25519)"
PUBKEY="$(tr -d '\r\n' < "$PUBKEY_PATH")"
[[ "$PUBKEY" == ssh-* ]] || die "$PUBKEY_PATH does not look like a public key"
PUBKEY_B64="$(printf '%s' "$PUBKEY" | base64 | tr -d '\n')"

info "onboarding users: ${ORDER[*]}"

for u in "${ORDER[@]}"; do
  IFS=: read -r alias path project <<<"${ENTRY[$u]}"

  # ------------------------------------------------- phase 1: root bootstrap ---
  info "[$u] phase 1/2: root bootstrap on $VPS_ROOT_SSH (idempotent)"
  R bash -s -- "$u" "$CPU_QUOTA" "$MEMORY_MAX" "$PUBKEY_B64" <<'ROOT_EOF'
set -euo pipefail
U="$1"; CPU="$2"; MEM="$3"; PUBKEY_B64="$4"

if id "$U" >/dev/null 2>&1; then
  echo "[vps] user $U already exists"
else
  useradd -m -s /bin/bash "$U"
  echo "[vps] created user $U"
fi

if command -v docker >/dev/null 2>&1; then
  echo "[vps] docker already installed"
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update -y && apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck disable=SC1091
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  echo "[vps] installed docker"
else
  echo "[vps] ERROR: no apt-get and no docker - install docker + the compose plugin manually, then re-run" >&2
  exit 1
fi

if id -nG "$U" | tr ' ' '\n' | grep -qx docker; then
  echo "[vps] $U already in docker group"
else
  # WARNING: docker group membership is root-equivalent (a member can bind-
  # mount the host filesystem via any container and escalate to root). This
  # is granted to EVERY LADDY_USERS entry because rootless docker is not yet
  # implemented (TODO [laddy-rootless-docker]). Before onboarding a second
  # user on a shared VPS: know that either user can already root the box -
  # this is not a per-user isolation boundary until rootless docker lands.
  usermod -aG docker "$U"
  echo "[vps] added $U to docker group (root-equivalent - see comment above)"
fi

UID_N="$(id -u "$U")"
SLICE_DIR="/etc/systemd/system/user-${UID_N}.slice.d"
if [ -f "$SLICE_DIR/limits.conf" ]; then
  echo "[vps] cgroup limits already set for $U"
else
  mkdir -p "$SLICE_DIR"
  cat > "$SLICE_DIR/limits.conf" <<EOF2
[Slice]
CPUQuota=$CPU
MemoryMax=$MEM
EOF2
  systemctl daemon-reload
  echo "[vps] cgroup limits set for $U (CPUQuota=$CPU MemoryMax=$MEM)"
fi

# Provision the local push pubkey into this user's authorized_keys as root,
# so the very first phase-2 (user-phase) SSH connection for a freshly
# created user can already authenticate - phase 2 must never be the first
# writer of this user's authorized_keys (chicken-and-egg otherwise).
install -d -m 700 -o "$U" -g "$U" "/home/$U/.ssh"
AUTH_KEYS="/home/$U/.ssh/authorized_keys"
[ -f "$AUTH_KEYS" ] || install -m 600 -o "$U" -g "$U" /dev/null "$AUTH_KEYS"
PUBKEY="$(printf '%s' "$PUBKEY_B64" | base64 -d)"
if grep -qF "$PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
  echo "[vps] pubkey already in $U's authorized_keys"
else
  echo "$PUBKEY" >> "$AUTH_KEYS"
  chown "$U:$U" "$AUTH_KEYS"
  echo "[vps] appended pubkey to $U's authorized_keys"
fi
ROOT_EOF

  # ---------------------------------------- build this user's env.vps locally ---
  # AGENT_REPO_URL/AGENT_WORK_ROOT use a literal $HOME - env.vps is sourced by
  # a shell on the VPS later (kickoff.sh), so it expands there, not here.
  ENV_CONTENT="$(cat "$ENGINE_DIR/env.vps.example")
# --- written by vps-onboard.sh (per-user, GitHub-free / bare-hub model) ---
AGENT_REPO_URL=\$HOME/repo_$project/hub.git
AGENT_WORK_ROOT=\$HOME/repo_$project
NTFY_TOPIC=$NTFY_TOPIC"
  # printf '%s\n' guarantees a trailing newline so a later hand-append (e.g.
  # TEST_COMMANDS) lands on its own line instead of gluing onto NTFY_TOPIC=...
  # and being silently swallowed into that variable's value.
  ENV_CONTENT_B64="$(printf '%s\n' "$ENV_CONTENT" | base64 | tr -d '\n')"

  # ------------------------------------------------------- phase 2: as user ---
  info "[$u] phase 2/2: toolchain + bare hub + engine checkout + env.vps (idempotent)"
  ssh -o BatchMode=yes "$alias" true 2>/dev/null || die "[$u] user-phase ssh failed - did the root phase run and does ~/.ssh/config have Host $alias with the right key?"
  ssh -o BatchMode=yes "$alias" bash -s -- "$path" "$project" "$ENV_CONTENT_B64" <<'USER_EOF'
set -euo pipefail
ENGINE_PATH="$1"; PROJECT="$2"; ENV_B64="$3"
need() { command -v "$1" >/dev/null 2>&1; }

need python3 || echo "[vps] WARNING: python3 not found - install manually" >&2
need git     || echo "[vps] WARNING: git not found - install manually" >&2
need docker  || echo "[vps] WARNING: docker not found in this shell yet - log out/in to pick up the new group, or install manually" >&2
need claude  || echo "[vps] WARNING: claude CLI not found - install + 'claude login' manually" >&2
need codex   || echo "[vps] NOTE: codex CLI not found (optional - only needed for the rw2 cross-vendor guard)" >&2

HUB="$HOME/repo_$PROJECT/hub.git"
if [ -d "$HUB" ]; then
  echo "[vps] hub already exists: $HUB"
else
  mkdir -p "$(dirname "$HUB")"
  git init --bare "$HUB" >/dev/null
  echo "[vps] created bare hub: $HUB"
fi

if [ -d "$ENGINE_PATH/.git" ]; then
  echo "[vps] engine checkout already exists: $ENGINE_PATH"
else
  mkdir -p "$ENGINE_PATH"
  git init -b main "$ENGINE_PATH" >/dev/null
  echo "[vps] created empty engine checkout: $ENGINE_PATH"
fi
# Converge HEAD to refs/heads/main even on re-runs against a box that was
# provisioned before this script pinned the initial branch name - `git init`
# on an EXISTING repo does not move HEAD, so this must run unconditionally.
git -C "$ENGINE_PATH" symbolic-ref HEAD refs/heads/main
git -C "$ENGINE_PATH" config receive.denyCurrentBranch updateInstead

ENV_FILE="$ENGINE_PATH/env.vps"
if [ -f "$ENV_FILE" ]; then
  echo "[vps] $ENV_FILE already exists - leaving it alone (edit by hand if needed)"
else
  printf '%s' "$ENV_B64" | base64 -d > "$ENV_FILE"
  echo "[vps] wrote $ENV_FILE"
fi

echo "[vps] phase 2 done for $ENGINE_PATH (project=$PROJECT)"
USER_EOF

  info "[$u] done"
done

echo
echo "[onboard] done: ${ORDER[*]}"
echo
echo "This script never touches GitHub and never seeds the engine or hub content -"
echo "that's two manual, Director-run steps per user:"
echo

for u in "${ORDER[@]}"; do
  IFS=: read -r alias path project <<<"${ENTRY[$u]}"
  echo "  # $u ($alias, project=$project)"
  echo "  scripts/upgrade_laddy.sh $u"
  echo "  git remote add laddy ssh://$alias/home/$u/repo_$project/hub.git && git push laddy main"
  echo "  (second command: run from the '$project' project's local checkout)"
  echo
done

echo "Phone: subscribe to ntfy topic '$NTFY_TOPIC' if you set one."
