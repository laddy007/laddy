# VPS onboarding / adding a project

`laddy` is GitHub-free by design: the VPS never holds a GitHub
credential of any kind — no deploy key, no `gh`, no clone of a
`github.com` remote. Every task branch reaches the VPS by the Director
pushing from their own machine to a **local bare hub** that lives on
the VPS. "Onboard a new project" and "onboard a new VPS user" are the
same operation — one `LADDY_USERS` entry per (VPS user, target
project) pair.

This file is a practical runbook. Everything here is either
`scripts/vps-onboard.sh` (root + per-user bootstrap, run from your
machine over SSH) or a handful of Director-run follow-ups it cannot do
itself (nothing here needs a GitHub credential — it's all local system
setup plus the seed pushes you already control).

---

## 1. Prerequisites

- **SSH access** to the box as root (or a user with root privileges).
  The onboarder's root phase provisions the unprivileged user's login
  key for you (appends your local pubkey to their
  `~/.ssh/authorized_keys` as root), so there's no separate initial-
  login step to arrange. The only thing you must set up yourself is
  the local `~/.ssh/config` `Host` entry the per-user phase connects
  through — it must exist *before* you run the script, four lines:

  ```
  Host vps-laddy
    HostName <vps-ip-or-hostname>
    User laddy
    IdentityFile ~/.ssh/id_ed25519
  ```

  (`Host` = the `ssh_alias` you give `vps-onboard.sh`; `User` = the
  `LADDY_USERS` unix user; `IdentityFile` = the same keypair you point
  the onboarder at below.)
- **A local SSH keypair** you're willing to grant push access to the
  VPS user's bare hub (`ssh-keygen -t ed25519` if you don't have one).
- `ssh` on your machine; the target VPS reachable and running Debian/
  Ubuntu (or already has Docker + the compose plugin installed if it
  isn't).

## 2. Configure `vps.conf`

`scripts/vps-onboard.sh` asks for everything it needs on first run and
saves it to `<engine-dir>/vps.conf` (git-ignored, like `env.local` /
`env.vps`) — re-runs are non-interactive. To onboard a **second**
project or user, edit `vps.conf` by hand and add another
`LADDY_USERS` entry (or delete the file to be asked everything again).
Schema (also documented in `vps.conf.example`):

```
LADDY_USERS="user:ssh_alias:engine_path:project ..."
```

- `user` — the unprivileged system user on the VPS (never root).
- `ssh_alias` — an `~/.ssh/config` Host entry for that user.
- `engine_path` — absolute path to that user's engine checkout
  (e.g. `/home/laddy/laddy`).
- `project` — target project name; that user's bare hub lives at
  `~/repo_<project>/hub.git`.

Multiple entries are space-separated; parsing is shared between
`vps-onboard.sh` and `upgrade_laddy.sh` (`scripts/lib/laddy_users.sh`)
so they can never drift.

## 3. Run the onboarder

```bash
scripts/vps-onboard.sh
```

Idempotent and interactive; safe to re-run after a partial failure
(every remote step checks before creating). For each `LADDY_USERS`
entry:

- **Phase 1 (root, over `VPS_ROOT_SSH`):** creates the unix user if
  missing, installs Docker + the compose plugin if missing, adds the
  user to the `docker` group (⚠ root-equivalent — see the loud warning
  in the script; there is no per-user isolation between two onboarded
  users on the same box until rootless docker lands), writes a
  systemd slice with `CPUQuota` / `MemoryMax` from `vps.conf`, and
  appends your local pubkey to the new user's `~/.ssh/authorized_keys`
  (as root — the user has no login path of its own yet, so this can't
  happen in phase 2).
- **Phase 2 (as the user, over their own SSH alias):** requires phase 1
  to have already run for this user (that's what makes the SSH alias
  authenticate at all); checks for `python3` / `git` / `docker` /
  `claude` / `codex` (warns, doesn't install, on anything missing),
  creates the bare hub (`~/repo_<project>/hub.git`) if it doesn't
  exist, creates an empty engine checkout at `engine_path` on `main`
  with `receive.denyCurrentBranch=updateInstead` (so a later
  `git push` updates the working tree in place), and writes `env.vps`
  from `env.vps.example` plus the per-user `AGENT_REPO_URL` /
  `AGENT_WORK_ROOT` / `NTFY_TOPIC`.

It does **not** touch GitHub and does **not** seed the engine or the
target project's content — that's two Director-run follow-ups it
prints at the end, one pair per onboarded user:

```bash
scripts/upgrade_laddy.sh laddy       # from THIS engine repo
scripts/push-hub.sh laddy            # from the TARGET repo (seed its hub)
```

## 4. Seed the engine and the target

```bash
# from THIS engine repo's local checkout:
scripts/upgrade_laddy.sh <user>            # promote local main into ~/laddy on the VPS

# from the TARGET project's local checkout:
scripts/push-hub.sh <user>                 # adds the `laddy` remote from
                                            # vps.conf + seeds the hub (idempotent)
```

`upgrade_laddy.sh` is all-or-nothing across whatever users you name (or
every `LADDY_USERS` entry with no args): if any of them has a loop
running or a dirty `~/laddy`, nothing is upgraded. It only ever touches
the engine checkout — never the hub, never target content.

## 5. Seed the target's `.laddy/` directory

A target project needs its own `.laddy/{specs,tasks,docker,security}`
committed before the first task can run:

- `.laddy/specs/` — where you author task specs (`create-spec` skill or
  by hand).
- `.laddy/tasks/` — the loop commits per-task artifacts here; starts
  empty (a `.gitkeep` is enough).
- `.laddy/docker/` and `.laddy/security/` — the containerized gate
  config the loop and `merge-verified.sh` both run. Copy this engine
  repo's `docker/` and `security/` as your starting point and commit
  them into the target repo; adjust `docker/compose.test.yml` /
  `security/semgrep.yml` for the target's own stack (test DB, service
  image, language-specific rules) as needed.

## 6. Activate the skills locally

Claude Code discovers skills in `.claude/skills/`, so symlink or copy
them once, in the **target** repo:

```bash
ln -s <engine-checkout>/skills/create-spec .claude/skills/create-spec
ln -s <engine-checkout>/skills/investigate .claude/skills/investigate
```

## 7. Phone notifications (optional)

`vps-onboard.sh` asks for an `NTFY_TOPIC` and writes it into each
user's `env.vps`; subscribing your phone to that topic is a separate,
manual step (ntfy app or web, no credential involved).

## 8. Smoke-check before trusting it

```bash
ssh <ssh_alias> '~/laddy/scripts/kickoff.sh' 2>&1 | head -1   # should print the usage line
scripts/smoke-review-cli.sh                                   # locally: least-privilege review CLIs run to completion
```

Then give it a trivial real task end-to-end (see `USAGE.md`) before
trusting it with anything that matters.

---

## What the Director must never delegate to the onboarder

- Anything requiring a GitHub credential — there isn't one on this box,
  by design. If a step seems to need one, stop; it's a sign of scope
  creep back toward the retired deploy-key topology.
- Root-level provisioning beyond phase 1 above (creating additional
  system users, changing cgroup/systemd limits by hand) — re-run
  `vps-onboard.sh` with an updated `vps.conf` instead of hand-editing
  the VPS.
- Registering/subscribing the ntfy topic on your phone — the script
  only writes the topic name into `env.vps`.

See `USAGE.md` for the day-to-day task lifecycle once a project is
onboarded.
