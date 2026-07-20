# Merge hold: fullrun-s2  (blast L3, broken)

## What failed

- local full test suite is red
- gate infra changed by this branch was NOT verified - the gate ran trusted main's copy of: .laddy/security/semgrep.yml
- security panel blocker(s): Rule B's fake-guard bypass is NOT closed: any unrelated, never-true `if <expr>.st_nlink ...: raise` before the truncate still suppresses the rule, contradicting the commit message that claims the class is fixed.; Rule A's O_NOFOLLOW/O_EXCL exemption is a textual lookahead over the whole flags expression, so a conditional or dead mention of either token disables the rule on a genuinely unguarded write open.; Rule A incorrectly treats bare O_EXCL as symlin... [truncated]

## Security panel findings

- Rule B's fake-guard bypass is NOT closed: any unrelated, never-true `if <expr>.st_nlink ...: raise` before the truncate still suppresses the rule, contradicting the commit message that claims the class is fixed.
- Rule A's O_NOFOLLOW/O_EXCL exemption is a textual lookahead over the whole flags expression, so a conditional or dead mention of either token disables the rule on a genuinely unguarded write open.
- Rule A incorrectly treats bare O_EXCL as symlink protection.
- Rule B accepts inverted, partial, or unrelated hard-link guards.

## Local test failure (tail)

```
equires_semgrep def test_rule_b_fires_on_ftruncate_without_nlink_fixture() -> None: found = _scan(FIXTURES / "rule_b_bad_ftruncate.py") > assert _lines(found, RULE_B, "rule_b_bad_ftruncate.py") E AssertionError: assert set() E + where set() = _lines(set(), 'python-ftruncate-without-nlink-check', 'rule_b_bad_ftruncate.py') tests/test_semgrep_fsrules.py:82: AssertionError ================================ tests coverage ================================ _______________ coverage: platform linux, python 3.11.15-final-0 _______________ Coverage XML written to file coverage.xml =========================== short test summary info ============================ FAILED tests/test_semgrep_fsrules.py::test_not_encoded_classes_are_documented FAILED tests/test_semgrep_fsrules.py::test_severity_behaviour_recorded_and_new_rules_are_error FAILED tests/test_semgrep_fsrules.py::test_rule_a_fires_on_unguarded_write_open_fixture FAILED tests/test_semgrep_fsrules.py::test_rule_a_reports_the_two_unguarded_sites_as_findings FAILED tests/test_semgrep_fsrules.py::test_two_ruleset_copies_are_byte_identical FAILED tests/test_semgrep_fsrules.py::test_rule_b_fires_on_ftruncate_without_nlink_fixture 6 failed, 820 passed in 35.85s ------------- Diff Coverage Diff: 0e927db8e3fd4cff8f85034bead98653ffd5a0ba...HEAD, staged and unstaged changes ------------- No lines with coverage information in this diff. ------------- @@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0
```

## What is needed

This branch changes the gate's own infrastructure, which the gate
restores from trusted main before it runs, so re-running does not clear it:
the next run restores the same paths. No gate here can judge the branch's
own copy - landing those paths is your call, on a route you trust.

Any red gate above may be the restore's doing rather than a defect:
the suite ran against main's infra, not this branch's.

`fullrun-s2` is NOT merged and NOT deleted.
