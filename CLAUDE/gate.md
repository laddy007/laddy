# Agent guide: the gate, docker & scanners

Read this when touching the gate, the docker test setup, or the scanners. Core
rule (CLAUDE.md, Trust direction + Def of done): the deterministic gate is the
binding correctness bar, and it must stay reproducible on both the VPS
(pre-filter) and the trusted machine (authoritative re-run).

## Two gates

- **Fast inner gate** -- the quick suite the loop runs each iteration.
- **Authoritative containerized gate** (`orchestrator/testgate.py`,
  `DockerGate`) -- the binding correctness gate. The same image builds on the
  VPS and locally, so a change here affects both nodes.

## The container

`docker/Dockerfile.test` + `docker/compose.test.yml` define the gate; the
dogfood copy laddy runs against itself lives in `.laddy/docker/`.

- The gate command is **injected** by `testgate.py` via `eval "$GATE_COMMAND"`
  -- it is NOT hardcoded in compose, so policy stays in Python.
- `pip check` fails the image build on dependency inconsistency; app deps are
  pinned.

## Scanners are pinned -- no autopilot bumps

The gate runs `semgrep` + `gitleaks` + diff-coverage. Their versions are pinned
(`semgrep` via pip, `gitleaks` a sha256-verified release binary). A bump
changes which findings the gate reports, so a silent update can move or break
the gate under you. Bump **deliberately, never on autopilot**: if you automate
dependency updates, have them open PRs for review -- do not automerge scanner
bumps. After any bump, rebuild the image and confirm the gate still runs green.
Weakening the SAST ruleset (`.laddy/security/semgrep.yml`) is a sensitive
(L3 / security-surface) change.

## WSL footgun

A gate run that fails **instantly** with
`docker-credential-desktop.exe: exec format error` (before even pulling the
base image) means `~/.docker/config.json` has a leftover
`"credsStore": "desktop.exe"` from Windows Docker Desktop. Remove that key. An
empty `auths: {}` is fine for anonymous public pulls; the Windows credential
helper does not run under Linux/WSL docker anyway.

## Oracle (pointer)

The oracle (`orchestrator/oracle/`) is a **post-merge, non-blocking** subsystem
that measures the gates' escape rate (defects that passed every gate). It never
blocks a merge and is not part of the go/no-go decision. Runbook: `USAGE.md`.
