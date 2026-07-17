# Merge hold: fullrun-s2  (blast L3, broken)

## What failed

- local full test suite is red
- security panel blocker(s): security panel member 'claude' did not return a valid verdict; holding for human review; The hard-link protection rule is trivially bypassed by an unrelated or post-truncation st_nlink access.

## Security panel findings

- security panel member 'claude' did not return a valid verdict; holding for human review
- The hard-link protection rule is trivially bypassed by an unrelated or post-truncation st_nlink access.

## Local test failure (tail)

```
inux -- Python 3.11.15 /usr/local/bin/python3.11

    def test_two_ruleset_copies_are_byte_identical() -> None:
>       assert RULESET.read_bytes() == MIRROR.read_bytes()
E       AssertionError: assert b'# Offline s...(...), ...)\n' == b'# Offline s...        ...\n'
E         
E         At index 704 diff: b'\n' != b'#'
E         Use -v to get more diff

tests/test_semgrep_fsrules.py:129: AssertionError
================================ tests coverage ================================
_______________ coverage: platform linux, python 3.11.15-final-0 _______________

Coverage XML written to file coverage.xml
=========================== short test summary info ============================
FAILED tests/test_semgrep_fsrules.py::test_not_encoded_classes_are_documented
FAILED tests/test_semgrep_fsrules.py::test_severity_behaviour_recorded_and_new_rules_are_error
FAILED tests/test_semgrep_fsrules.py::test_rule_a_fires_on_unguarded_write_open_fixture
FAILED tests/test_semgrep_fsrules.py::test_rule_b_fires_on_ftruncate_without_nlink_fixture
FAILED tests/test_semgrep_fsrules.py::test_rule_a_reports_the_two_unguarded_sites_as_findings
FAILED tests/test_semgrep_fsrules.py::test_two_ruleset_copies_are_byte_identical
6 failed, 692 passed in 38.42s
-------------
Diff Coverage
Diff: 42aa9e8be8b6f6de2ac6d66c6d11be37be14fd81...HEAD, staged and unstaged changes
-------------
No lines with coverage information in this diff.
-------------

@@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0
```

## What is needed

This change is not mergeable as-is. Re-run the task on the VPS to fix
the failing gate(s), or address them and push a new revision of the
branch. `fullrun-s2` is NOT merged and NOT deleted.
