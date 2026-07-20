# Merge hold: fullrun-s2  (blast L3, broken)

## What failed

- local full test suite is red
- gate infra changed by this branch was NOT verified - the gate ran trusted main's copy of: .laddy/security/semgrep.yml
- security panel blocker(s): Rule B encodes the hard-link force-overwrite class only for os.ftruncate(fd, 0); the equivalent and more idiomatic O_TRUNC-on-open form evades both new rules, and unlike every other gap in this ruleset it is NOT disclosed in the header's NOT-ENCODED list — so the ruleset asserts mechanical coverage of a class it only partially covers.; Rule A is trivially bypassed by a guard token that evaluates to zero.; Rule B accepts an st_nlink guard for an unrelated... [truncated]

## Security panel findings

- Rule B encodes the hard-link force-overwrite class only for os.ftruncate(fd, 0); the equivalent and more idiomatic O_TRUNC-on-open form evades both new rules, and unlike every other gap in this ruleset it is NOT disclosed in the header's NOT-ENCODED list — so the ruleset asserts mechanical coverage of a class it only partially covers.
- Rule A is trivially bypassed by a guard token that evaluates to zero.
- Rule B accepts an st_nlink guard for an unrelated file descriptor.

## Local test failure (tail)

```
inux -- Python 3.11.15 /usr/local/bin/python3.11 def test_two_ruleset_copies_are_byte_identical() -> None: > assert RULESET.read_bytes() == MIRROR.read_bytes() E AssertionError: assert b'# Offline s...(...), ...)\n' == b'# Offline s...ate($FD, 0)\n' E E At index 704 diff: b'\n' != b'#' E Use -v to get more diff tests/test_semgrep_fsrules.py:129: AssertionError ================================ tests coverage ================================ _______________ coverage: platform linux, python 3.11.15-final-0 _______________ Coverage XML written to file coverage.xml =========================== short test summary info ============================ FAILED tests/test_semgrep_fsrules.py::test_not_encoded_classes_are_documented FAILED tests/test_semgrep_fsrules.py::test_severity_behaviour_recorded_and_new_rules_are_error FAILED tests/test_semgrep_fsrules.py::test_rule_a_fires_on_unguarded_write_open_fixture FAILED tests/test_semgrep_fsrules.py::test_rule_a_reports_the_two_unguarded_sites_as_findings FAILED tests/test_semgrep_fsrules.py::test_rule_b_fires_on_ftruncate_without_nlink_fixture FAILED tests/test_semgrep_fsrules.py::test_two_ruleset_copies_are_byte_identical 6 failed, 820 passed in 25.32s ------------- Diff Coverage Diff: dbf5189a0a7a4a7255fd44ba0e8565adfc9f85af...HEAD, staged and unstaged changes ------------- No lines with coverage information in this diff. ------------- @@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0
```

## What is needed

This branch changes the gate's own infrastructure, which the gate
restores from trusted main before it runs, so re-running does not clear it:
the next run restores the same paths. No gate here can judge the branch's
own copy - landing those paths is your call, on a route you trust.

Any red gate above may be the restore's doing rather than a defect:
the suite ran against main's infra, not this branch's.

`fullrun-s2` is NOT merged and NOT deleted.
