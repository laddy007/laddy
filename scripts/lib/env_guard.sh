# scripts/lib/env_guard.sh - refuse to source a git-TRACKED env file (H-D7-1).
#
# env.local / env.vps are `set -a; source`d on the Director's TRUSTED machine
# (merge-verified.sh / push-hub.sh), so any shell in them runs as the Director.
# The legitimate file is ALWAYS gitignored/untracked (see .gitignore); a TRACKED
# env.* is therefore an injection vector - a task branch that `git add -f`s one
# and merges it would get it executed on the next local run. Refuse it, loudly.
#
# Defense-in-depth behind the primary fix (env.* is engine-sensitive -> L3, so
# such a file never auto-merges in the first place). Quiet on the normal path:
# an untracked env file (or a non-git engine dir) sources as before.
#
# Usage: laddy_refuse_tracked_env <engine_dir> <env_file>
#   Call BEFORE `source <env_file>`. Dies (exit 1) iff git positively reports
#   the file tracked; returns 0 otherwise (untracked, or not a git repo).
laddy_refuse_tracked_env() {
  local engine_dir="$1" env_file="$2"
  if git -C "$engine_dir" ls-files --error-unmatch -- "$env_file" >/dev/null 2>&1; then
    echo "ERROR: refusing to source git-tracked env file '$env_file'." >&2
    echo "       The legitimate env.* is gitignored/untracked; a tracked one is" >&2
    echo "       a code-execution injection vector. Remove it from git and keep" >&2
    echo "       the real env file untracked." >&2
    exit 1
  fi
}
