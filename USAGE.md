# laddy — user guide

How to give a task to the autonomous dev-loop and get a reviewed, tested,
merged change back. This is the **practical "how do I use it"** guide.
For one-time VPS / project setup read `setup.md`; for the engineering
detail read the code (`orchestrator/`) — this repo does not carry a
separate design-doc tree.

---

## What it is, in one paragraph

You write a small spec describing a change, in a **target** project repo
(e.g. `myapp`) — `laddy` itself is just the engine, installed per VPS
user and pointed at that target. A cheap **VPS worker** implements the
change autonomously — a developer agent writes code, runs tests, and two
reviewers (one Claude, one Codex) critique it in a loop until it
converges — and pushes the result as a task branch to a **bare hub that
lives on the VPS itself** (never to GitHub, never to `main`). Then, on
**your own machine**, you run one command that re-verifies everything
from scratch on trusted infrastructure and merges it into `main`. The
split is the whole point: the VPS does the work but you never have to
trust it — the binding gate (full test re-run + security review) runs
where the agent cannot reach, and the VPS holds no GitHub credential of
any kind.

```
   YOU write spec ─► VPS: implement <-> test <-> review <-> review ─► push <task> to the hub
                                                                             │
   YOU run merge-verified.sh ◄─────────────────────────────────────────────┘ (fetch the hub)
        │ re-run tests + security panel on YOUR machine (trusted)
        ├─ safe + green ─► merges after you type the exact task id
        ├─ sensitive    ─► risk summary; type the exact task id to merge
        └─ broken/flag   ─► tells you what failed and why (no merge)
   YOU push main to GitHub, and to the hub (so the next task starts current)
```

---

## Quick flow — the whole loop, copy-paste

Two tracks: **manual** (today) and **fullrun** (one command, once its driver
lands — currently being built, slice S3). Commands below use this deployment:
local engine at `/mnt/c/myprogramfiles/laddy` (machine DELLi), VPS user `laddy`
(ssh alias `vps-laddy`, engine `~/laddy`, hub `~/repo_laddy/hub.git`). For
another user swap `laddy` for their `LADDY_USERS` entry.

### A. Manual flow (today)

```bash
# 1. Author the spec — locally: hand-write .laddy/specs/<task>.md
#    (or skip to step 4 with `kickoff.sh <task> --new` to co-author on the VPS).

# 2. Spec onto the hub (skip if you used --new):
git add .laddy/specs/<task>.md && git commit -m "spec: <task>"
./scripts/push-hub.sh laddy

# 3. Only if you changed ENGINE code (orchestrator/, scripts/): refresh the VPS engine.
#    (Pure spec/target changes don't need this — the loop clones the target from the hub.)
./scripts/upgrade_laddy.sh laddy

# 4. Kick off on the VPS — ALWAYS in tmux: clarify/design gates run foreground
#    and an SSH drop kills them before the loop detaches (only the loop is nohup'd).
ssh vps-laddy
tmux new -A -s <task>                       # attach-or-create
cd ~/laddy && ./scripts/kickoff.sh <task>   # + --skip-clarify to skip Q&A
#   → answer clarify, and "Approve this approach?" for a high-risk design gate
#   → wait for "[kickoff] loop detached"; then Ctrl-b d (detach tmux), close SSH
#   reconnect anytime:  ssh vps-laddy && tmux attach -t <task>
```
```bash
# 5. Watch (from local). Real progress is the iteration-log; the .log gains
#    [loop] heartbeat lines once round 1 finishes. Ends at stop_before_merge.
./scripts/watch-vps.sh <task>

# 6. Merge (local, from the repo, FOREGROUND — it ends with an interactive y/N):
./scripts/merge-verified.sh <task>          # re-runs gates on trusted infra
#   → MERGE into local main, or HOLD with a merge-hold.md digest
#   → final "push main to origin + delete hub branch? (y/N)" — conscious choice,
#     N keeps it local-only (never pre-answer y)

# 7. Keep the hub current for the next task:
./scripts/push-hub.sh laddy
```

### B. fullrun (once it lands — being built, slice S3)

One **local** command wraps steps 2–7: `push → kickoff on VPS → poll → rw3
(trusted cross-vendor review that feeds findings back to the developer) →
merge-or-hold`, looping until merged/held, and **pausing + ntfy** at the human
gates (L3 design approval, L3 merge confirm, the GitHub-push y/N).

```bash
./scripts/fullrun.sh <task>       # one task
./scripts/fullrun.sh <project>    # all of a project's ready tasks
./scripts/fullrun.sh              # = all: every project's every ready task
```

Both `fullrun` and `kickoff` will **auto-wrap in tmux** (shared
`scripts/lib/tmux_wrap.sh`, on the TODO), so step 4's manual tmux goes away.

---

## Before you start (once)

Setup is a one-time thing per VPS user, done by the Director — see
`setup.md`: root bootstrap (unix user, docker, cgroup slice), a
per-user bare hub (`~/repo_<project>/hub.git`), an empty engine checkout
promoted by `scripts/upgrade_laddy.sh`, and the target project's `main`
seeded onto the hub. On **your** machine you only need Docker running
plus `git` / `python3` / the `claude` and `codex` CLIs — the merge
gate's scanners (`diff-cover`, `semgrep`, `gitleaks`) and its test
Postgres run **inside the container**, not on your host. The VPS never
holds a GitHub credential — merging, and pushing to GitHub, only ever
happen on your machine.

---

## Task lifecycle

### 1. Author the spec

Plain Markdown at `<target>/.laddy/specs/<task>.md`. Optional front
matter picks the task type / roles:

```markdown
---
type: feature        # feature | bug | spike | audit | investigate
---
# Add a "share on Bluesky" button to the play summary

## Goal
...
## Acceptance
...
```

`type: audit` / `investigate` are **report-only** tasks — the deliverable
is a findings report, not code, and it merges under a stricter path guard
(only its own task artifacts, spec drafts, and `docs/` may change).

Co-author it with the **`create-spec` skill** (interactive Claude Code
session on your machine), or write it directly on the VPS with
`kickoff.sh <task> --new` (step 2) — in that case skip straight there,
nothing needs to reach the hub first.

### 2. Get the spec onto the hub

```bash
git add .laddy/specs/<task>.md && git commit -m "..."
scripts/push-hub.sh <user>   # commit locally, then sync main -> hub
```

The VPS worktree is cloned from the hub's `main`, so a spec that only
exists in your local `main` is invisible to it. (`--new`, above, skips
this — the spec is written straight into the fresh task worktree.)

### 3. Kick off the VPS

```bash
ssh vps-laddy '~/laddy/scripts/kickoff.sh <task>'
ssh vps-laddy '~/laddy/scripts/kickoff.sh <task> --new'   # co-write the spec first
```

Two gates run **interactively** in this SSH session, in order:

1. **Clarify** — Claude reads the spec against the real target code and
   asks any blocking questions in the terminal ("no questions" just
   proceeds).
2. **Design** — foreground for high-risk tasks; a no-op otherwise. A
   rejection here stops `kickoff.sh` before anything detaches.

Then the loop **detaches** (`nohup`) and runs unattended — the *loop*
survives an SSH drop. The two gates above do **not**: they run foreground,
so a dropped SSH kills them before the loop ever detaches and the task
silently never starts. **Run `kickoff.sh` inside `tmux`** (`tmux new -A -s
<task>`) so a drop leaves the gates running — reconnect with `tmux attach
-t <task>`. This matters most for high-risk tasks, whose design gate blocks
on "Approve this approach?" and cannot be answered once the pipe is gone.

### 4. Watch it

```bash
scripts/watch-vps.sh <task>                    # from your machine, or:
ssh vps-laddy 'tail -f ~/agent-logs/<task>.log'
```

The loop: **developer → fast tests → reviewer 1 (Claude) → reviewer 2
(Codex)**, bouncing back to the developer whenever a test fails or a
reviewer asks for changes, until it converges (max 4 rounds; a repeated
deadlock escalates to a senior reviewer). One phone notification (ntfy)
fires when it finishes.

**Where it ends up:**

| Terminal state | Meaning |
|---|---|
| pushed `<task>` to the hub | Converged. Ready for you to merge (step 6). |
| `CAP_REACHED` / `ESCALATED_DEADLOCK` | Did **not** converge. Nothing pushed; a `handback.md` on the VPS explains what was tried. |

All artifacts (the iteration log, reviewer verdicts, a human summary,
`merge-decision.json`) live under `.laddy/tasks/<task>/` on the branch.

### 5. Batch several tasks (optional)

`kickoff.sh` runs one task. To hand the VPS a batch (e.g. overnight),
enqueue the ready specs and drain the queue — a single-flight FIFO
runner that takes each task to a terminal state, then moves to the
next. Every queued task runs with `--skip-clarify`, so **answer the
clarify gate up front** — a queued run started at 3am has nobody to ask.
Run these on the VPS (or over SSH), inside the engine checkout:

```bash
python -m orchestrator.run --phase enqueue task-a task-b task-c   # explicit (all-or-nothing)
python -m orchestrator.run --phase enqueue --all                  # every ready spec
python -m orchestrator.run --phase enqueue --pick                 # choose interactively
python -m orchestrator.run --phase queue-list                     # what is pending

# drain it, detached for an overnight batch:
nohup python -m orchestrator.run --phase queue >> ~/agent-logs/queue.log 2>&1 &
```

The queue lives under `AGENT_WORK_ROOT` (runtime state, never
committed). A failed task is **not** re-queued — it produces the same
ntfy + `handback.md` as a direct run and the runner moves on; each
converged task still pushes its own `<task>` branch to the hub. Merge
them exactly as below — `merge-verified.sh` with no args processes
every ready branch at once.

### 6. Merge it (on your machine, from the target repo)

Pull nothing, trust nothing from the VPS — just run, from inside the
**target repo's** local checkout:

```bash
scripts/merge-verified.sh              # every ready branch on the hub
scripts/merge-verified.sh mytask       # or specific tasks
```

(`scripts/merge-verified.sh` here means wherever this engine repo is
checked out on your machine — run it with your shell's working
directory set to the target repo; it operates on `--repo .`.)

For each branch it re-derives everything on **your** machine: recomputes
the policy from your trusted code (not the branch's), re-runs the
**full test suite** + coverage + `semgrep` + `gitleaks`, and re-runs the
cross-vendor reviewer plus a **security panel**. Then it acts by how
risky the change is:

- **Safe (docs/i18n) or ordinary logic, all gates green** → **merges** into
  your local `main` after you confirm by typing the **exact task id** (a
  wrong or blank id declines and merges nothing).
- **Touches a sensitive surface** (auth, migrations, `models.py`, deploy, …),
  gates green → **asks you**: prints *what* is sensitive and a one-screen
  summary, then you type the **exact task id** to merge. You make a risk
  call — you don't read the diff.
- **Something failed** (a test, coverage, a scanner, a security/reviewer
  blocker) → **broken**: prints *what failed, why, and what is needed*, and
  does **not** offer to merge. Fix it by re-running the task on the VPS; the
  tool never edits code itself.

When it finishes, if anything merged it asks:

```
push main to origin and delete N merged branch(es)? (y/N)
```

`y` pushes your `main` to **GitHub** and deletes the merged task
branches from the hub (pure git — the commits are already in `main`, so
only unmerged/held branches stay in the list). `N` leaves everything
local and prints the `git push origin main` command for you.

Held branches (declined risk decisions, broken changes) are **never**
deleted — they wait for you, with a `merge-hold.md` digest in
`.laddy/tasks/<task>/`.

**Dry run:** add `--no-input` to see what *would* auto-merge without
prompting or pushing (it holds every sensitive change and never pushes).

### 7. Keep the hub current

`merge-verified.sh` only pushes `main` to **GitHub**. It never touches
the hub's `main` — do that yourself:

```bash
scripts/push-hub.sh <user>
```

This is not required for the tripwire (below) to stay quiet — a hub
`main` that is simply *behind* your local `main` is still an ancestor of
it. It matters because `kickoff.sh` clones its task worktree base from
the hub: skip this and the next task starts from stale code.

### 8. Correct the ask and resume a finished task

A task that stopped — hit the iteration cap (`CAP_REACHED`), deadlocked
(`ESCALATED_DEADLOCK`), or landed at `stop_before_merge`/`PUSHED` — is
**sticky**: a plain re-kickoff no-ops. When the reason it stopped is that the
**spec was wrong** (the code was fine, the ask was incomplete), there is one
explicit, logged way to put it back to work:

```bash
# 1. Edit the spec ON THE TASK BRANCH with your own editor and push it:
git fetch laddy <task> && git checkout <task>
$EDITOR .laddy/specs/<task>.md          # fix the ask
git commit -am "spec: <task> — add the missing requirement" && git push laddy <task>

# 2. Resume, with a mandatory note saying what you changed:
scripts/kickoff.sh <task> --resume \
  --reason "spec was missing throttling; added it + replay protection"
```

`--resume` first syncs the VPS's task worktree to the branch tip on the hub, so
the correction you pushed in step 1 is what the developer actually reads (and
what the recorded `spec_sha` receipt hashes) — you edit and push from your own
clone; the VPS picks it up. The `--reason` note is prepended to the **next
developer round** (on top of the reviewer verdict that stopped it, not instead
of it), so the developer reads both the corrected ask and where it got stuck. One `--resume` buys exactly one
run to the next terminal; resume again to keep going (unbounded, but every
resume is logged and the count + latest reason show up in the handback). A
`PATH_GUARD_VIOLATION` is **not** resumable — that tree carries forbidden edits;
discard the branch and restart instead.

Resume changes nothing about trust: it re-arms iteration only. The resumed run
re-traverses rw1/rw2/the authoritative gate exactly as a fresh one, never pushes
to origin, and never skips a reviewer (see SECURITY.md).

---

## The tripwire (what fires, and what to do)

Every `merge-verified.sh` run fetches the hub first and checks: **is
the hub's `main` an ancestor of your local `main`?** If not — the hub's
`main` carries a commit your local `main` never produced — the VPS (or
something with hub push access) wrote where only you may write. The
**whole run aborts**: zero branches are merged, even ones that would
otherwise have been clean.

```
[TRIPWIRE] hub main is NOT an ancestor of local main.
The VPS must never write main - this is suspicion of an
unauthorized write. NOTHING was merged. Inspect the
hub (laddy) and ~/laddy on the VPS, then decide.
```

**What to do:** don't re-run and hope it clears. Inspect the hub
directly (`git log` against `ssh://<alias>/home/<user>/repo_<project>/hub.git`)
and the VPS user's `~/laddy` for anything that shouldn't be there — a
manual push, a compromised key, a bug in the loop that somehow wrote
`main`. This is a Director decision, not something to script around;
once you understand what happened, either force the hub's `main` back
to match your trusted history or accept the divergence deliberately
before merging anything else.

---

## `upgrade_laddy.sh` contract

`scripts/upgrade_laddy.sh [user...]` promotes **this engine repo's**
local `main` into `~/laddy` for one or more `LADDY_USERS` entries. No
args = every user in `vps.conf`.

- **All-or-nothing preflight.** Before touching anything, it checks
  every named user for `busy-loop` (an `orchestrator.run` process is
  running for them) or `dirty-tree` (their `~/laddy` has uncommitted
  changes). If **any** named user fails, **nothing** is upgraded —
  it prints which users are upgradable and which are busy/dirty, and
  you re-run naming a clear subset if you want a partial upgrade.
- **Promotion is a plain git push** to `ssh://<alias><path> main:main`;
  the VPS side has `receive.denyCurrentBranch=updateInstead`, so the
  checkout updates in place — no separate `git pull` step on the VPS.
- It only ever touches the **engine** checkout (`~/laddy`), never the
  target's hub or task branches — those are the Director's separate
  `scripts/push-hub.sh <user>` (seeding/keeping-current) and `kickoff.sh`
  (task work).

---

## Concurrency rules

- **Engine upgrades vs. running loops.** `upgrade_laddy.sh`'s
  all-or-nothing preflight refuses to promote a user whose loop is
  mid-run (or whose checkout is dirty) — you cannot swap the engine out
  from under an in-flight task.
- **One queue drain per node.** `orchestrator.run --phase queue` takes
  an `O_EXCL` lock file under `AGENT_WORK_ROOT`; a second drain attempt
  on the same node fails fast rather than double-processing the FIFO.
  Enqueueing (`--phase enqueue`) is safe to run anytime — it just adds
  to the file-backed queue.
- **`main` is a reserved task id.** `kickoff.sh` refuses `main` outright
  (the hub is a closed namespace: every branch except `main` is a task,
  so a task literally named `main` would be indistinguishable from the
  base branch).
- **The merge tripwire is whole-run, not per-branch.** A diverged hub
  `main` aborts `merge-verified.sh` entirely (see above) — even branches
  that would individually verify clean are not merged, because the hub
  itself is no longer trusted for that run.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `kickoff.sh` errors "missing spec" | The spec never reached the hub. `scripts/push-hub.sh <user>` from the target repo, or re-run with `--new`. |
| `kickoff.sh` says the design gate was not approved | A high-risk task's design gate was rejected in the interactive session; the loop never detached. Revise the spec/design and retry. |
| A task reached the cap / deadlocked | It did not converge — read `handback.md` on the branch, refine the spec, and re-run. Nothing was pushed. |
| `merge-verified.sh` aborts with `[TRIPWIRE]` | Hub `main` diverged from local `main` — see "The tripwire" above. Do not re-run blind. |
| `merge-verified.sh` holds everything | You ran with `--no-input` (holds every sensitive change), or Docker isn't running / the gate image failed to build (a scanner missing *inside the image* counts as a finding). Start Docker and re-run; the scanners are in the container, not on your host. |
| A branch "no longer applies cleanly" | A prior merge moved `main`; re-run the task on the VPS to rebuild it against the new `main` (make sure you `scripts/push-hub.sh <user>` first so the VPS clones the current base). |
| `upgrade_laddy.sh` refuses to upgrade | Preflight found a busy loop or a dirty `~/laddy` for a named user — see "Concurrency rules". |

---

## Reference

- **Setup (once per VPS user / new project):** `setup.md`.
- **Roles (agent prompts):** `roles/` (developer, rw1, rw2, security,
  senior-reviewer, explorer, debugger, verify, investigator).
- **The engine:** `orchestrator/` (Python).
- **Skills (interactive helpers):** `skills/` — `create-spec`
  (spec brainstorming) and `investigate` (diagnosis-only session).

---

## Oracle — measuring the gates' escape rate (post-merge, non-blocking)

The gates decide; the oracle measures. It never blocks anything. Design
rationale lives in commit history and `oracle/classes.md`'s own
comments — there is no separate design doc in this split repo.

**Trigger (automated):** every `orchestrator.local_merge` run ends with an
`[oracle]` notice when a review is due (>= 5 agent merges since the
watermark, any L3 merge, or 14 days — whichever comes first). Standalone:
`python -m orchestrator.oracle status` (exit 1 = due). Run oracle
commands from inside the **target repo** (`--repo .` by default), same
as `merge-verified.sh`.

**Runbook (the run itself is manual by design, Director-driven):**

1. `python -m orchestrator.oracle scope` — what the next run reviews
   (calibration mode: all L2 + L3, every 5th L1). First run ever: add
   `--since <sha>` (e.g. the last externally reviewed commit). `scope`
   prints the resolved endpoint sha (`main` at the time of this call) and
   a `record-run --to <sha>` hint - COPY that sha: the manual AI-review
   session below can span hours, and a merge landing in that window must
   not be silently counted as reviewed.
2. Per reviewed task: `python -m orchestrator.oracle prepare <task>` —
   materializes a CLEAN worktree (task artifacts stripped; enforced by
   test) and writes `oracle-<task>-phase1.md` / `-phase2.md` prompt files
   into `../<target>-oracle-work/`.
3. Paste phase 1 into a FRESH session (never inside the loop). Only after
   its findings are final, paste phase 2 (attribution).
4. Per finding: `python -m orchestrator.oracle escape <task> --class-slug
   <slug> --grade confirmed|plausible --summary ... --evidence ...
   [--gate test|rw1|rw2|merge-rw|coverage-gap|dev-scaffold]`. Slugs come
   from `oracle/classes.md`; a new class needs a registry commit
   FIRST. Commit the appended iteration log directly to local main
   (append-only history; push stays with the Director).
5. `python -m orchestrator.oracle record-run --to <sha from step 1>` —
   appends the `oracle-run` event (advances the watermark, records
   reviewed/skipped per bucket = the honest denominator). `--to` is
   REQUIRED and must be the SAME sha `scope` printed in step 1: a
   record-time default (`main`) would silently include merges nobody
   reviewed. Commit it.
6. `python -m orchestrator.oracle ledger` — per-class counts + escape-rate
   series. A RECURRENT class (>= 2 escapes) is a confirmed upgrade target;
   route the fix per the ladder (test > contract > create-spec > policy >
   role prompt > guide). A prompt/role fix is NEVER validated by
   "the text is there" — it needs a seeded-bug eval (see below).
7. When the fix + distillation-to-test lands:
   `python -m orchestrator.oracle resolve <task> <flag-id> --note "<commit/test>"`.
   The Director dismisses a plausible finding that did not hold:
   `... resolve <task> <flag-id> --resolution dismissed --note "why"`.

Open oracle-escape flags = the backlog of unfixed escapes
(`python -m orchestrator.oracle ledger`).

### Seeded evals — validating a prompt/role fix (the honesty rule)

A fix in a role prompt / create-spec / .md guide is validated by a
seeded eval, never by "the text is there". The harness replants a known
defect into a sandboxed loop run (local bare hub, `eval/*` branch
namespace the merge tool structurally ignores, developer role disabled)
and checks mechanically whether the gates now catch it. Run it where the
loop runs (the VPS): the sandbox reuses `TEST_COMMANDS` and the agent
CLIs.

1. `python -m orchestrator.oracle eval-new <task> <flag-id> --eval-id <slug>`
   — scaffolds `.laddy/oracle/evals/<slug>/` from a recorded escape:
   original spec as the cover story, the shipped merge diff as the seed
   starting point. **Trim `seed.patch` to the minimal defect** and fill
   `expected.json` `files` (the paths carrying the bug — reviewer blockers
   are anchored to them).
2. `python -m orchestrator.oracle eval-check <slug>` — mechanical
   validation, zero agent tokens (spec parses, class registered, patch
   applies on main, expected files present in the patch). Commit the
   bundle (it is policy-sensitive: `.laddy/oracle/*` → L3 by design).
3. Commit the candidate fix (prompt/role/.md), then
   `python -m orchestrator.oracle eval-run <slug> --fix-ref <fix-sha>`
   (`--base <branch>` to validate a fix branch before merge; `--docker`
   for the full authoritative gate; `--keep` to inspect the sandbox;
   `--no-record` to skip appending the `seeded-eval` event — dry run;
   `--work-root <dir>` to point the sandbox elsewhere — default
   `<repo-parent>/<target>-eval-work`, shared with `eval-check`).
   - exit 0 / CAUGHT — the fix closes the class: resolve the escape flag
     with `--note "fix <sha>; eval <slug>"` and commit the appended
     `seeded-eval` event in `.laddy/oracle/run-log.jsonl`.
   - exit 1 / MISSED — the gates still wave the defect through: the fix
     is NOT validated, iterate.
   - INCONCLUSIVE — the chain broke or blockers landed outside
     `expected.files`; re-run with `--keep` and inspect, or widen
     `expected.files` / trim the seed.

The planted bug can never merge: the sandbox's `origin` is a throwaway
local hub, branches live in `eval/*` (invisible to
`orchestrator.local_merge` — test-pinned), and the eval spec never exists
under `.laddy/specs/` in the real repo.

**v1 interpretation caveat.** The sandbox worktree still contains the
committed bundle under `.laddy/oracle/evals/<id>/` and the branch is
named `eval/<id>` — a reviewer that goes hunting can find the answer
key. Treat a CAUGHT whose blocker text references the eval bundle, the
seed, or `.laddy/oracle/evals` as INVALID: re-run with `--keep` and
inspect the verdicts before trusting it. Structural stripping of the
bundle from the sandbox base is a candidate hardening, deliberately
deferred for v1.
