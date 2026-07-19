---
type: feature
---
# todo-phone: 2026-07-19 TODO batch + laddy-phone PWA

## Goal

Land the 2026-07-19 TODO batch on top of fix/merge-flow-queue:

- pinned requirements-dev.txt,
- tmux self-wrap for kickoff (scripts/lib/tmux_wrap.sh, LADDY_NO_TMUX),
- target-first env.local resolution in merge-verified,
- [gate] progress output,
- stub-spec refresh on kickoff --new reuse,
- remote-ask channel (LADDY_ASK_REMOTE=1 file+ntfy) for the interactive gates,
- O_NOFOLLOW on the iteration-log append,
- phone/ - the laddy-phone token-gated PWA (stdlib server, LADDY_PHONE_TOKEN,
  tailnet bind, scripts/phone.sh, systemd unit).

## Acceptance

- Full suite green (pytest -n auto), ruff clean, basedpyright 0 errors.
- Director-authored branch; judged via merge-verified --local on the trusted
  machine.
