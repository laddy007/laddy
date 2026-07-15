# laddy engine — Director-side (trusted machine) notes

## Never auto-push to GitHub

`scripts/merge-verified.sh` ends with an interactive `y/N` prompt before
pushing local `main` to `origin` and deleting merged branches from the VPS
hub. Pushing to GitHub is a separate, explicit Director decision (trust-model
spec, Tier 3) — never automatic, even right after a clean local merge.

- Always run this script in the foreground so that prompt can reach a real
  user. Never background it or pipe its output through another command in a
  way that detaches stdin — that turns the prompt into a silent `EOFError`
  (safe, but not the point) or, worse, invites "fixing" it by piping in an
  answer.
- Never pre-answer `y` (e.g. `echo y | ./scripts/merge-verified.sh`) without
  the user explicitly confirming, for that specific run, that they want
  local main pushed to origin right now.

## Local gate needs python3.11

`orchestrator/target_policy.py` imports `tomllib` (Python 3.11+). This
machine's default `python3` is 3.10. `env.local` sets `PYTHON_BIN=python3.11`
for this repo — a bare `python3` fallback crashes on import before argparse
even runs.

## Docker credential helper (WSL)

If a gate run fails immediately with `docker-credential-desktop.exe: exec
format error` (before even pulling the base image), `~/.docker/config.json`
has a leftover `"credsStore": "desktop.exe"` from Windows Docker Desktop —
remove that key. An empty `auths: {}` is fine for pulling public images
anonymously; the credential helper isn't needed and the Windows binary
doesn't run under Linux/WSL docker anyway.
