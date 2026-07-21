# Merge hold: create-spec  (blast L3, broken)

## What failed

- local full test suite is red
- security panel blocker(s): The new trusted-machine launcher can start Claude inside an attacker-controlled task branch without removing repository agent hooks or steering configuration.; The untrusted diff contains prior-approval claims capable of steering the binding security review.

## Security panel findings

- The new trusted-machine launcher can start Claude inside an attacker-controlled task branch without removing repository agent hooks or steering configuration.
- The untrusted diff contains prior-approval claims capable of steering the binding security review.

## Local test failure (tail)

```
_reason_and_detaches() -> None: # --resume must reach `--phase resume` (forwarding --reason via REST) and # detach the same way the loop does (nohup, unbuffered, heartbeat) so it # survives an SSH drop. Guard the wiring against a silent regression. text = (_SCRIPTS / "kickoff.sh").read_text(encoding="utf-8") resume = next(ln for ln in text.splitlines() if "--phase resume" in ln) > assert "nohup" in resume, resume E AssertionError: LADDY_LOG_HEARTBEAT=1 setsid --fork "$PY" -u -m orchestrator.run "$TASK" --phase resume ${REST[@]+"${REST[@]}"} >> "$LOG" 2>&1 < /dev/null E assert 'nohup' in ' LADDY_LOG_HEARTBEAT=1 setsid --fork "$PY" -u -m orchestrator.run "$TASK" --phase resume ${REST[@]+"${REST[@]}"} >> "$LOG" 2>&1 < /dev/null' tests/test_kickoff_wiring.py:33: AssertionError ================================ tests coverage ================================ _______________ coverage: platform linux, python 3.11.15-final-0 _______________ Coverage XML written to file coverage.xml =========================== short test summary info ============================ FAILED tests/test_kickoff_wiring.py::test_kickoff_resume_forwards_reason_and_detaches 1 failed, 1145 passed in 40.35s ------------- Diff Coverage Diff: b1c0e45b03c01373a2bb71a9d44c4f724bd4234a...HEAD, staged and unstaged changes ------------- No lines with coverage information in this diff. ------------- @@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0
```

## What is needed

This change is not mergeable as-is. Re-run the task on the VPS to fix
the failing gate(s), or address them and push a new revision of the
branch.

Or fix it right here on the trusted machine and re-judge locally:
commit the fix ON TOP of this branch with ordinary git, then run
`merge-verified.sh <task> --local <ref>` (a sha, branch, or worktree
path). --local does not trust the code more - it trusts the route:
you are the trusted author and the same applicable gate still judges
the diff (the historical VPS artifact attestation is N/A),
and the judged sha is the merged sha, so nothing unverified
slips in. It is a stopgap until bounce-to-VPS exists (and a
legitimate escape hatch after).

`create-spec` is NOT merged and NOT deleted.
