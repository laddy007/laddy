# Merge hold: fullrun-s2  (blast L3, broken)

## What failed

- local full test suite is red
- gate infra changed by this branch was NOT verified - the gate ran trusted main's copy of: .laddy/security/semgrep.yml
- security panel blocker(s): Rule B (python-ftruncate-without-nlink-check) is defeated by a single no-op line: its pattern-not-inside suppresses on any mention of `.st_nlink` in the enclosing function, regardless of whether that mention guards the truncate or even precedes it.; Rule A (python-open-without-nofollow-or-excl) matches only the literal flags expression, so any write-open whose flags are bound to a variable — the ordinary style for conditional flags — silently escapes the... [truncated]

## Security panel findings

- Rule B (python-ftruncate-without-nlink-check) is defeated by a single no-op line: its pattern-not-inside suppresses on any mention of `.st_nlink` in the enclosing function, regardless of whether that mention guards the truncate or even precedes it.
- Rule A (python-open-without-nofollow-or-excl) matches only the literal flags expression, so any write-open whose flags are bound to a variable — the ordinary style for conditional flags — silently escapes the rule.
- Rule A incorrectly treats O_EXCL without O_CREAT as a symlink-safe write open.
- Rule B can be bypassed by an unrelated or ineffective st_nlink access.

## Local test failure (tail)

```
inux -- Python 3.11.15 /usr/local/bin/python3.11 def test_two_ruleset_copies_are_byte_identical() -> None: > assert RULESET.read_bytes() == MIRROR.read_bytes() E AssertionError: assert b'# Offline s...(...), ...)\n' == b'# Offline s... ...\n' E E At index 704 diff: b'\n' != b'#' E Use -v to get more diff tests/test_semgrep_fsrules.py:129: AssertionError ================================ tests coverage ================================ _______________ coverage: platform linux, python 3.11.15-final-0 _______________ Coverage XML written to file coverage.xml =========================== short test summary info ============================ FAILED tests/test_semgrep_fsrules.py::test_not_encoded_classes_are_documented FAILED tests/test_semgrep_fsrules.py::test_severity_behaviour_recorded_and_new_rules_are_error FAILED tests/test_semgrep_fsrules.py::test_rule_b_fires_on_ftruncate_without_nlink_fixture FAILED tests/test_semgrep_fsrules.py::test_rule_a_fires_on_unguarded_write_open_fixture FAILED tests/test_semgrep_fsrules.py::test_rule_a_reports_the_two_unguarded_sites_as_findings FAILED tests/test_semgrep_fsrules.py::test_two_ruleset_copies_are_byte_identical 6 failed, 820 passed in 27.11s ------------- Diff Coverage Diff: 69f797dfe7bb8a39d2b546500cf8dd822e6a45e8...HEAD, staged and unstaged changes ------------- No lines with coverage information in this diff. ------------- @@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0
```

## What is needed

This branch changes the gate's own infrastructure, which the gate
restores from trusted main before it runs, so re-running does not clear it:
the next run restores the same paths. No gate here can judge the branch's
own copy - landing those paths is your call, on a route you trust.

Any red gate above may be the restore's doing rather than a defect:
the suite ran against main's infra, not this branch's.

`fullrun-s2` is NOT merged and NOT deleted.
