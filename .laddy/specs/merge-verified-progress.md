---
type: feature
roles: [developer, rw1, rw2]
risk: low
---
# merge-verified-progress — show per-stage progress during the local merge

## Goal
Give the Director a live sense of *which stage is running* while
`merge-verified.sh` (i.e. `orchestrator.local_merge`) verifies a branch. Today
the launching terminal is silent for minutes at a time and looks hung; the
long quiet periods are the deterministic Docker gate and the codex-driven
security panel / rw2 re-review, all of which run as subprocesses with captured
output. Emit short, prefixed stage markers so the silence is explained and the
Director can tell "still working" from "stuck".

Explicitly NOT in scope: streaming the raw tool output (pytest dots, codex
tokens) into the terminal. That would mean teeing the ShellRunner and
AgentRunner instead of capturing, which risks corrupting the `tests_tail` /
verdict digests that the merge decision depends on. This task is stage markers
only.

## Root-cause context (why it is silent)
`orchestrator/local_merge.py::gather_gates` runs the whole per-branch pipeline
linearly — worktree checkout → policy/merge check → blast-radius
classification → `_binding_on_merged_tree` (the Docker gate) → `run_security_panel`
→ `_rw2` — and every step captures its subprocess output for the digest, so
nothing reaches the terminal until the final `[merge]/[hold]/[push]` summary in
`main()`. `Engine.run` loops over tasks with no per-task announcement either.

## Scope
In: `orchestrator/local_merge.py` (stage markers inside `gather_gates`; a
per-task start line in `Engine.run`) and their tests under `tests/`.
Out: any change to what is captured, to the `tests_tail`/`scan_findings`/verdict
digests, to `decide()`, or to the merge/push/hold behaviour; no new
dependencies; no change to `stdout` content.

## Acceptance criteria
1. `gather_gates` emits a human-readable progress line **to stderr** before
   each meaningful stage, each prefixed `[merge] <task_id>: `. At minimum:
   fetch/worktree, blast-radius result (e.g. `blast = L2`), deterministic gate
   start, security panel start (with the reviewer count), and rw2 re-review
   start. Each line is flushed (unbuffered), so it appears in real time under
   `nohup`/redirection, not at the end.
2. All progress goes to **stderr**; `stdout` (the verdict digest printed by
   `on_verdict` / `main`) stays **byte-for-byte identical** to today. Any
   existing test that captures `stdout` must keep passing unchanged.
3. `Engine.run` emits one `[merge] <task_id>: verifying (N of M)` style line to
   stderr as it starts each ready task.
4. A test asserts the stage markers appear (via `capsys`/`capfd`) for a
   representative L2 branch (gate + security + rw2 all exercised via the
   existing fakes), AND asserts the captured `stdout` for that run is unchanged
   from the pre-change behaviour.
5. No behavioural change: gate results, blast classification, the merge/hold
   decision, and the GitHub-push `y/N` flow are all identical. The suite
   (`ruff`, `basedpyright`, `pytest`) stays green.

## Notes
- Follow the existing print convention already in this file: `[merge]`,
  `[push]`, `[oracle]`, `[TRIPWIRE]` prefixes. Keep the new lines in the same
  `[merge] <task>: …` family.
- `local_merge.py` is trusted merge-authority code; expect this to classify as
  a sensitive (L3) surface at merge time and need a human risk decision even
  though the change is additive logging. That is fine — keep the diff minimal
  and obviously side-effect-free so the decision is easy.
