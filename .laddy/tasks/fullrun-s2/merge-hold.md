# Merge hold: fullrun-s2  (blast L3, broken)

## What failed

- local full test suite is red
- gate infra changed by this branch was NOT verified - the gate ran trusted main's copy of: .laddy/security/semgrep.yml
- security panel blocker(s): Rule B2 (python-open-trunc-without-nlink-check) exempts any flags expression containing O_EXCL without also requiring O_CREAT. Since O_EXCL is a POSIX no-op without O_CREAT — the exact fallacy commit 55058bc8 fixed in Rule A — adding a single inert flag suppresses both Rule B2 and (via O_NOFOLLOW) Rule A, leaving the force-overwrite hard-link hole undetected by the blocking gate.; Rule B2 can be bypassed with O_EXCL without O_CREAT, even though that comb... [truncated]

## Security panel findings

- Rule B2 (python-open-trunc-without-nlink-check) exempts any flags expression containing O_EXCL without also requiring O_CREAT. Since O_EXCL is a POSIX no-op without O_CREAT — the exact fallacy commit 55058bc8 fixed in Rule A — adding a single inert flag suppresses both Rule B2 and (via O_NOFOLLOW) Rule A, leaving the force-overwrite hard-link hole undetected by the blocking gate.
- Rule B2 can be bypassed with O_EXCL without O_CREAT, even though that combination does not protect an existing regular file.
- Rule B2 accepts an nlink check on any unrelated fd; an nlink check of the O_TRUNC target cannot safely occur after open because truncation is immediate.

## Local test failure (tail)

```
rror: assert b'# Offline s...(...), ...)\n' == b'# Offline s...FLAGS, ...)\n' E E At index 704 diff: b'\n' != b'#' E Use -v to get more diff tests/test_semgrep_fsrules.py:155: AssertionError ================================ tests coverage ================================ _______________ coverage: platform linux, python 3.11.15-final-0 _______________ Coverage XML written to file coverage.xml =========================== short test summary info ============================ FAILED tests/test_semgrep_fsrules.py::test_not_encoded_classes_are_documented FAILED tests/test_semgrep_fsrules.py::test_severity_behaviour_recorded_and_new_rules_are_error FAILED tests/test_semgrep_fsrules.py::test_rule_a_fires_on_unguarded_write_open_fixture FAILED tests/test_semgrep_fsrules.py::test_rule_b_fires_on_ftruncate_without_nlink_fixture FAILED tests/test_semgrep_fsrules.py::test_rule_a_reports_the_two_unguarded_sites_as_findings FAILED tests/test_semgrep_fsrules.py::test_rule_b2_fires_on_open_trunc_without_nlink_fixture FAILED tests/test_semgrep_fsrules.py::test_rule_b2_reports_queue_py_lock_pid_write_as_a_finding FAILED tests/test_semgrep_fsrules.py::test_two_ruleset_copies_are_byte_identical 8 failed, 821 passed in 39.93s ------------- Diff Coverage Diff: 043cbd9660e8b602fffbb50394f393bc3e7c4db8...HEAD, staged and unstaged changes ------------- No lines with coverage information in this diff. ------------- @@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0
```

## What is needed

This branch changes the gate's own infrastructure, which the gate
restores from trusted main before it runs, so re-running does not clear it:
the next run restores the same paths. No gate here can judge the branch's
own copy - landing those paths is your call, on a route you trust.

Any red gate above may be the restore's doing rather than a defect:
the suite ran against main's infra, not this branch's.

`fullrun-s2` is NOT merged and NOT deleted.
