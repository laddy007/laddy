# Tasks for the VPS (run via kickoff)

Operational notes: how to launch the next tasks on the VPS worker. Kickoff
syntax reminder: `./scripts/kickoff.sh <task-id> --new` -- the task id comes
FIRST, `--new` after it. `--new` opens an INTERACTIVE Claude session in the
task worktree where the Director co-authors the spec; there is (currently) no
way to pass the description on the command line -- see task 2 below to fix that.

Prerequisite for `--new`: `claude` must be installed AND logged in on the VPS
(the authoring step runs `claude` as an interactive TUI). If it is missing the
session ends immediately with "authoring session added nothing".

---

## Task 1: nonretryable-agent-errors

Launch:

```bash
./scripts/kickoff.sh nonretryable-agent-errors --new
```

Then paste this brief into the interactive authoring session (it writes the
spec; do NOT implement here, spec only):

    Write a spec for this task (spec only, no implementation).

    Problem: orchestrator/agent_retry.py::request_payload spends the entire
    retry budget even on failures no retry can fix. An expired CLI login / auth
    error returns exit_reason="error"; request_payload runs it max_retries+1
    times with the identical result and the whole security panel abstains. Root
    cause is established -- no exploration needed.

    A clean signal exists: auth/login failures have recognizable text ("OAuth
    session expired", "Failed to authenticate", "Invalid API key", "not logged
    in") -- pattern-matchable exactly like orchestrator/agents.py::detect_quota
    already does for quota.

    Goal: classify non-retryable failures (a new exit_reason category in
    _error_kind, or have request_payload recognize them and fail immediately)
    so an auth error consumes at most 1 attempt. Declared follow-up to
    agent-error-visibility.

    AC: an auth-error runner consumes <=1 attempt (today it burns
    max_retries+1); a regression test pins it; existing retry behavior on
    transient errors is unchanged.

Locus is orchestrator/agent_retry.py + orchestrator/agents.py (NOT in
security_globs), so it rides L1/L2: developer -> rw1 -> rw2 -> fast tests ->
binding gate, then `merge-verified --local nonretryable-agent-errors <branch>`.

---

## Task 2: kickoff-new-brief

Let a new task carry its brief on the command line so the brief PRE-LOADS the
interactive authoring session. The Director types `kickoff <task> --new
"<brief>"`; the authoring Claude opens already knowing the assignment, drafts
the spec from it, and the Director then refines it interactively. NOT headless:
the interactive co-authoring stays -- the brief seeds the prompt instead of a
blank one the Director must type into.

Launch (author this spec the current way -- the feature does not exist yet):

```bash
./scripts/kickoff.sh kickoff-new-brief --new
```

Then paste this brief into the authoring session:

    Write a spec for this task (spec only, no implementation).

    Goal: `kickoff <task> --new "<brief>"` feeds <brief> INTO the spec-authoring
    prompt, so the interactive Claude session opens pre-loaded with the
    Director's assignment and drafts the spec from it; the Director then refines
    it interactively. The session stays interactive -- the brief pre-loads it,
    it does not replace it, and it is not a headless run.

    Today (root cause established, no exploration needed):
    - scripts/kickoff.sh runs `orchestrator.run <task> --phase new` with NO
      extra args, so a string after --new never reaches authoring; it lands in
      REST and later breaks `--phase clarify` ("requires exactly one task id").
    - orchestrator/run.py::_phase_new seeds `# {task_id}\n`, then runs
      author_spec, which launches `claude` with
      SPEC_AUTHOR_PROMPT.format(spec_rel=...) -- the prompt has no slot for a
      brief.

    Change (touch points scoped):
    - scripts/kickoff.sh: capture the optional token right after `--new` (target
      UX is `kickoff <task> --new "<brief>"`) and forward it as
      `orchestrator.run <task> --phase new --brief "<brief>"`. Routing it
      through a --phase-new flag means it never reaches clarify, which also
      fixes the stray-positional break above.
    - orchestrator/run.py argparse: add `--brief` (takes a value).
    - orchestrator/run.py::_phase_new: when --brief is given,
      1. seed the spec `# {task_id}\n\n{brief}\n` (instead of the bare headline),
      2. thread the brief into author_spec,
      3. compare against THAT seed for the "authoring added nothing" guard.
    - SPEC_AUTHOR_PROMPT + _default_author_spec: thread the brief through and add
      an optional block, e.g. "The Director's brief for this task:\n<brief>\n
      Draft the spec from it; ask only about what it leaves open." With no brief
      the prompt is byte-for-byte today's.

    AC:
    - `kickoff <task> --new "<brief>"` opens an INTERACTIVE authoring session
      whose Claude prompt contains <brief>, and does NOT error at clarify.
    - `kickoff <task> --new` with no brief behaves exactly as today.
    - the spec seed contains the brief when given; the empty-authoring guard
      still fires when the session adds nothing beyond the seed.
    - unit tests: kickoff forwards the brief only to --phase new; _phase_new
      seeds with the brief and threads it into the author prompt; the no-brief
      path is unchanged.

Locus: scripts/kickoff.sh + orchestrator/run.py (not in security_globs; expect
L1/L2). Verify with `merge-verified --local kickoff-new-brief <branch>`.
