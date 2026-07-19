# Security

## `--dangerously-skip-permissions`

This tool runs Claude Code with `--dangerously-skip-permissions`, meaning the agent can **read, write, delete, and execute files, and run git operations (including push) without per-action confirmation**. It also runs unattended (`nohup`-detached loop) once started.

**Use only on:**
- a machine/VPS you fully control,
- a repository you can afford to have force-modified or broken,
- with backups / git history you can roll back.

**Do not store any credentials on that VPS other than the Claude Code and Codex CLI auth themselves** (no cloud provider keys, no other services' API keys/tokens, no SSH keys to unrelated systems, no `.env` secrets for other projects). Because the agent runs with permission checks disabled, it can read and potentially act on any credential present on the box — limiting what's stored there limits the blast radius.

The maintainer is not responsible for data loss, unwanted commits/pushes, or unintended actions taken by the agent while running unattended. Review `MAX_LOOPS`, `QUOTA_MAX_WAIT_HOURS`, and the target-policy gates before first run.

## Where `--dangerously-skip-permissions` actually runs

This flag is only used **inside the VPS sandbox** — a disposable, unprivileged box with no access to anything outside itself (no GitHub credential, no deploy key, no `gh` CLI; see the topology diagram in `README.md`). It never runs on your local machine.

All code review, merge decisions, and the final push to GitHub happen on **your local machine**, where Claude Code should keep normal permission prompts enabled — do not add `--dangerously-skip-permissions` to your local `CLAUDE_CMD`/`CODEX_CMD`.

Merging is expected to be a human decision on your local machine: `scripts/merge-verified.sh` re-verifies everything (tests, coverage, semgrep, gitleaks, cross-vendor review) and asks for confirmation on anything sensitive, holding anything red. An automatic merge path does exist for safe/ordinary changes, but it only fires *after* that full local re-verification — it is your trusted machine's gate deciding, not the VPS merging anything on its own.

## The Director resume channel is not a trust weakening

`kickoff.sh <task> --resume` (the `director_resume` log event) un-sticks a
finished task and re-arms **iteration**, and nothing else. It is worth being
explicit about why this does not widen the trust model:

- **It re-arms iteration only.** A resumed task re-traverses rw1, rw2, and the
  authoritative Docker gate exactly as a fresh run — every gate is re-derived
  from the append-only log, not inherited. Resume cannot approve a review,
  cannot skip a reviewer, and cannot change a merge decision or a gate SHA.
- **It never publishes.** The resume path appends one log line and commits it
  to the task branch locally; it does **not** push to origin and does **not**
  merge. The local machine remains the sole merge authority (`merge-verified.sh`
  re-verifies everything on your box), and the hub's `main` is still write-only
  by you — the tripwire is unaffected.
- **A compromised VPS gains nothing new.** The VPS already writes the task log,
  so it could forge a `director_resume` and un-stick itself. The bounded
  consequence is that it buys **more of its own compute** — it still cannot
  merge, push to origin, or bypass a reviewer, all of which happen on your
  trusted machine and re-derive from scratch. No attestation is required for the
  mechanism to be safe; nothing downstream *branches* on the resume event (the
  recorded `spec_sha` is a receipt shown in the handback, never a gate input).
- **`PATH_GUARD_VIOLATION` is deliberately not resumable.** That terminal means
  a report-only task's branch carries source edits it was forbidden to make.
  Resuming would build on a poisoned tree, so the remedy is to discard the
  branch and restart — not to continue.

## Reporting a vulnerability

If you find a security issue in the *tool itself* (not "the agent did something risky because I ran it with skip-permissions on an untrusted target" — that's the documented, accepted risk model above), please **do not open a public issue**. Use GitHub's private security advisory feature on this repo instead, so it can be assessed before public disclosure.