I have a complete picture. All exploration done; the baseline is committed and the full suite (623 tests) is green. Let me compile the structured summary.

---

# Explorer summary — `enginefix-loop-hardening`

## Verdict
A tested baseline is already committed (`2784a37`). It implements all four functional changes correctly and the full suite (623 tests) is green. The **functional code holds** — I found no behavioral defect in the three hardening fixes. However, there are **two concrete test-coverage gaps that put acceptance criteria #1 and #5 at risk**, plus two minor robustness observations. The developer's job is primarily to **close the test gaps**, not to rewrite the fixes.

## Findings (baseline reviewed against acceptance criteria)

| AC | What baseline does | Status |
|----|--------------------|--------|
| 1 — env.vps trailing newline | `vps-onboard.sh:202` changed `printf '%s'` → `printf '%s\n'` (with explanatory comment). Correct: `$ENV_CONTENT` ends at `NTFY_TOPIC=<val>` with no newline; `printf '%s\n'` adds exactly one; base64 round-trips it; a later `>>`-appended `VAR=value` lands on its own line. | ✅ code / ❌ **no test** |
| 2 — stale-lock reclaim | `queue.py:114-162`: new `_pid_alive()` + `run_lock` reclaim branch. Dead/corrupt/absent pid → unlink + re-acquire; live pid → `QueueLocked`. | ✅ code + tests |
| 3 — observable detached run | `kickoff.sh:70` adds `-u` + `LADDY_LOG_HEARTBEAT=1`; `artifacts.py:151-165` mirrors each `append_log` to **stderr** (→ `$LOG` via `2>&1`), env-gated so CLI/tests stay quiet. | ✅ code + test |
| 4 — docs | `env.vps.example`: fast-vs-Docker gate note, venv-bootstrapping `TEST_COMMANDS` example, `AGENT_REPO_URL`/`AGENT_WORK_ROOT` commented to kill the duplicate-variable trap. | ✅ |
| 5 — no regression / diff-cov | 623 passed. But two **new** exception branches are untested (see below). | ⚠️ **at risk** |

## The two must-fix gaps

**G1 — AC#1 has no test (spec explicitly says "Test.").** The baseline diff touched only `test_queue.py` and `test_artifacts.py`; nothing exercises the env.vps newline. Recommend a reproduction test following the `test_kickoff_wiring.py` convention (static assertions on `scripts/`) *or*, stronger, a subprocess test that rebuilds the exact `ENV_CONTENT | base64` construction from `vps-onboard.sh:194-202`, decodes it, appends `TEST_COMMANDS=...`, `source`s it in bash, and asserts **both** `NTFY_TOPIC` and `TEST_COMMANDS` resolve independently (this reproduces the original glue-onto-NTFY_TOPIC bug and fails against the old `printf '%s'`).

**G2 — two new Python branches are uncovered → likely breaches `diff-cover --fail-under=90` (AC#5).** The authoritative gate runs `diff-cover coverage.xml --compare-branch=origin/main --fail-under=90` (`orchestrator/testgate.py:241-277`). These new lines have no covering test:
- `queue.py:128-129` — `except PermissionError: return True` in `_pid_alive` (foreign-owned pid). Test: `monkeypatch.setattr(os,"kill", raises PermissionError)` → assert `_pid_alive(pid) is True`.
- `queue.py:149-150` — `except (ValueError, OSError): old = 0` in `run_lock` (corrupt/empty lock file). Test: write `t1.lock` with non-numeric/empty contents, assert it's reclaimed.

Both fixes are ~3-line tests. Against ~21 new executable lines, 2–4 uncovered lines plausibly drops patch coverage to ~81–90% — right at/under the threshold. Cheap to close; do it rather than gamble on the boundary.

## Minor observations (note; not required by the criteria)

- **O1 — concurrent-reclaim raises raw `FileExistsError`, not `QueueLocked`.** `queue.py:156` re-opens with `O_EXCL` *outside* any `except`. If two loops reclaim the same stale lock simultaneously, the loser's `os.open` throws `FileExistsError`, which escapes `run_lock` — and `_phase_loop` (`run.py:351`) only catches `QueueLocked`, so the loop crashes with a traceback instead of a clean rc=4. Rare (requires two kickoffs racing right after a crash) and no worse than before in practice, but a one-line fix is clean: wrap that re-open in `try/except FileExistsError: raise QueueLocked(...)` — correct, because the winner is now the live holder.
- **O2 — `is_running` still reports a stale lock as "running".** `queue.py:165-166` only checks `.exists()`, so after a crash `_derive_status` (`run.py:236`) shows `running` for a dead task until the next `run_lock` reclaims it. The reclaim fixed the *blocking* but not the *status display*. Out of AC scope; flag only if you want status to match reclaim semantics (would mean routing `is_running` through `_pid_alive`).

## Affected files (scope confirmed matches spec)
- Code (already changed): `scripts/vps-onboard.sh`, `orchestrator/queue.py`, `orchestrator/artifacts.py`, `scripts/kickoff.sh`, `env.vps.example`
- Tests (changed): `tests/test_queue.py`, `tests/test_artifacts.py`
- Callers verified safe: `run.py:236` (`is_running`), `run.py:349` (`run_lock`) — signatures unchanged, only added a reclaim path. No cross-module break.

## Proposed approach for the developer
1. Keep all four functional changes as-is (they hold).
2. **Add G2 tests first** (PermissionError branch + corrupt-lock branch) — smallest, directly unblocks the diff-cover gate.
3. **Add G1 test** — subprocess reproduction of the env.vps newline (strongest form) or a static `scripts/`-text guard matching `test_kickoff_wiring.py`.
4. *Optional:* fix O1 (wrap the reclaim re-open) and add a heartbeat/`-u` wiring guard in `test_kickoff_wiring.py` to prevent silent regression of AC#3.
5. Re-run the full suite; confirm still green.

## Risks
- **Diff-coverage boundary (primary):** without G2 the change may fail the ≥90% patch-coverage gate in Docker even though local pytest is green — the failure surfaces only at the authoritative gate, not in `pytest -q`.
- **Shell fixes are untested by diff-cover** (bash, not measured), so AC#1 relies entirely on a hand-written test — if skipped, a future `printf '%s'` regression is invisible.
- **No new dependencies / no policy change** required — stays within spec's "Out" boundary.