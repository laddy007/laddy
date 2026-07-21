I now have a complete picture. Here is my scoping summary.

---

# Explorer Summary — `create-spec`

## Findings

**The task is almost entirely additive and low-risk.** `_phase_new` already does the full author→commit→push job correctly; the spec forbids touching its control flow. The work is two things: (1) a new thin launcher `scripts/create-spec.sh`, and (2) rewriting the `SPEC_AUTHOR_PROMPT` *text* in `orchestrator/run.py`. No bug to reproduce — this is a gap-fill.

**Handoff verified end-to-end (the spec's Notes ask for this).** The local→hub→VPS flow works as claimed:
- `_phase_new` (`orchestrator/run.py:229-230`) calls `gitops.commit_all` then `gitops.push(wt, task_id)` — pushing branch `<task>` (not `main`) to `origin`, which is the hub named by `AGENT_REPO_URL` in `env.local`.
- A later VPS `kickoff <task>` (no `--new`) runs `--phase clarify` → `task_worktree(task_id)` (`orchestrator/gitops.py:109-111`): it sees `origin/<task>` exists and **resumes that branch**, so the authored spec is picked up cleanly. The follow-up hint `kickoff <task>` (no `--new`) is correct — no adjustment to the guidance is needed.

**Exit-code propagation is free.** `main()` returns `_phase_new`'s rc directly for `--phase new` (`orchestrator/run.py:1191-1193`), surfaced via `raise SystemExit(main())`. Under `set -euo pipefail`, a non-zero exit aborts the launcher *before* the success hint — so AC#5 (hint only on success) and AC#6 (propagate refusals: "added nothing" / "already exists") both hold automatically if the hint is simply the last line after the python call.

## Affected files

- **`scripts/create-spec.sh`** (new) — thin launcher.
- **`orchestrator/run.py:79-86`** — rewrite the `SPEC_AUTHOR_PROMPT` string. This is the *only* product-code change and it is shared, so `kickoff --new` benefits too.
- **`env.local.example`** — optional doc line (spec says "if a knob needs mentioning"; likely none needed — no new knob).
- **`tests/`** — new test file(s), e.g. `tests/test_create_spec_script.py`, plus prompt-content assertions (extend `tests/test_run_cli.py` or add to a prompt test).

## Proposed approach

**Launcher `scripts/create-spec.sh`** — mirror `kickoff.sh`'s structure (`die`, `SCRIPT_DIR`/`ENGINE_DIR` from `BASH_SOURCE`), but:
1. Source `env.local` (not `env.vps`). **Key difference from kickoff:** kickoff sources `env.vps` only *if present* (`kickoff.sh:45`). AC#4 requires create-spec to **fail hard** when `env.local` is missing (`die` pointing at `local-onboard.sh` / `env.local.example`) — do *not* copy kickoff's silent-if-absent pattern here.
2. Validate `<task>` with the exact three checks from `kickoff.sh:39-41`: non-empty, `^[a-zA-Z0-9._-]+$`, and `!= main`.
3. `export PYTHONPATH="$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"`, `PY="${PYTHON_BIN:-python3}"`, verify `command -v "$PY"`.
4. Run **exactly** `"$PY" -m orchestrator.run "$TASK" --phase new` in the foreground — nothing else.
5. On success, `echo` the follow-up hint naming `kickoff <task>` (no `--new`) on the VPS.
- **Do NOT tmux-wrap** (kickoff wraps to survive SSH drop; this runs locally in the Director's terminal, like `local-task.sh` which has zero tmux). Keep it foreground and simple.

**Prompt rewrite** — enrich `SPEC_AUTHOR_PROMPT` per spec §"Enriched authoring prompt": front-matter fields (`type`, `roles`, `risk` low|medium|high, optional `status: draft-proposal` + one-line note that the loop refuses drafts), section skeleton (`# <task> — headline`, `## Goal`, root-cause/why, `## Scope` explicit In/Out, `## Acceptance criteria` numbered+testable, `## Notes`), the small/testable/slice-big-tasks (S0,S1,…) discipline, and target-generic naming (remove "myapp agent").

## Tests to write first (acceptance criteria → tests)

Follow the fake-python-stub subprocess pattern already in `tests/test_kickoff_wiring.py:73-112` (writes a recording `fake_python`, points `PYTHON_BIN` at it, asserts on captured argv):
- **AC#2** — stub captures argv: assert exactly one invocation, phase `new`, and *no* `clarify`/`design`/`loop` phase appears.
- **AC#1/#4** — run with a `tmp_path` cwd/env having **no `vps.conf`** and an env.local present → succeeds; with env.local *absent* → non-zero + clear message.
- **AC#3** — three cases (empty, invalid char, `main`) each non-zero with a message.
- **AC#5** — stdout contains the `kickoff <task>` hint on the stub-success path.
- **AC#7/#8** — string assertions on `SPEC_AUTHOR_PROMPT` (front-matter field names, section names, `draft-proposal`, discipline wording present; literal `"myapp"` absent). Scope AC#8's "no myapp" assertion to **the prompt string only** — `myapp` legitimately remains all over `tests/test_policy.py` as the sample target name.

## Risks / footguns

1. **`.format()` brace collision (highest-risk footgun).** `SPEC_AUTHOR_PROMPT` is consumed via `.format(spec_rel=spec_rel)` (`run.py:104`). Any literal `{`/`}` you add to the enriched template (unlikely in a Markdown spec skeleton, but watch for it) must be escaped `{{`/`}}` or `.format` raises `KeyError`/`ValueError` at authoring time. Keep `{spec_rel}` the sole replacement field. Add a test that `SPEC_AUTHOR_PROMPT.format(spec_rel="x")` doesn't raise.
2. **Existing prompt-guard tests.** `test_default_author_spec_no_brief_is_byte_identical` and `test_new_mode_no_brief_seed_and_prompt_unchanged` (`tests/test_run_cli.py:378,445`) assert against the `SPEC_AUTHOR_PROMPT` *constant* (and the seed), not a hardcoded literal — so changing the prompt text won't break them. The brief-append path (`SPEC_AUTHOR_BRIEF_BLOCK`) is separate and must stay untouched. Verify these still pass (spec Note explicitly asks).
3. **Side-effect-freedom of validation paths.** The three validation refusals and the missing-env.local refusal must exit *before* any python invocation — no worktree, no clone, no branch created on a rejected task name. `set -e` + ordering (validate → source → run) handles this; just don't invoke python before the guards.
4. **`--phase new` reserved-name check lives only in the launcher.** The orchestrator itself doesn't reject `main` for `--phase new`; the shell guard is the only gate. Mirror kickoff exactly so behavior matches.
5. **ASCII/LF discipline** (CLAUDE.md invariant): the enriched prompt is a long string — keep it ASCII (no smart quotes/em-dashes) and LF.

Nothing here touches the merge/trust path, `vps.conf`, VPS execution, or `_phase_new`'s control flow — all explicitly out of scope, and the proposed approach stays within those bounds.