# tmux_wrap.sh - self-wrap an interactive launcher in tmux (attach-or-create)
# so its foreground gates survive an SSH drop (TODO 2026-07: a dropped pipe
# killed kickoff's clarify/design gates before the loop detached - the task
# silently never started). Source this, then call, at the very top:
#
#   source "$SCRIPT_DIR/lib/tmux_wrap.sh"
#   laddy_tmux_wrap "<session-name>" "$@"
#
# No-op (returns 0, execution continues un-wrapped) when: already inside tmux
# ($TMUX set - the re-exec'd inner run lands here), stdout is not a TTY (a
# driver piping this script over ssh without -t never nests), tmux is absent,
# or LADDY_NO_TMUX is set (CI / headless escape hatch). Otherwise it execs
# `tmux new-session -A -s <name> -- <this script> <args>`: -A attaches to an
# existing session of that name (reconnect case) instead of double-starting.
laddy_tmux_wrap() {
  local session="${1:-laddy}"
  shift || true
  [ -z "${LADDY_NO_TMUX:-}" ] || return 0
  [ -z "${TMUX:-}" ] || return 0
  [ -t 1 ] || return 0
  command -v tmux >/dev/null 2>&1 || return 0
  exec tmux new-session -A -s "$session" -- "$0" "$@"
}
