# Merge hold: agent-error-visibility  (blast L3, broken)

## What failed

- local full test suite is red
- security panel blocker(s): Untrusted agent output from a failed run is now interpolated into the retry prompt under a 'rejected by the schema validator' framing, with no delimiting or untrusted-content marking - a prompt-injection channel that launders unparsed output into the next run, whose verdict IS trusted.; Raw agent stderr/stdout from authentication and quota failures is quoted verbatim, unredacted, into error text that is persisted to git-tracked artifacts and pushed - the... [truncated]

## Security panel findings

- Untrusted agent output from a failed run is now interpolated into the retry prompt under a 'rejected by the schema validator' framing, with no delimiting or untrusted-content marking - a prompt-injection channel that launders unparsed output into the next run, whose verdict IS trusted.
- Raw agent stderr/stdout from authentication and quota failures is quoted verbatim, unredacted, into error text that is persisted to git-tracked artifacts and pushed - the exact failure class most likely to contain a credential.
- Agent-controlled failed output is promoted into the trusted retry prompt, creating a security-panel prompt-injection path.
- Failed CLI output is re-sent without secret redaction and persisted in the human-facing merge report.

## Local test failure (tail)

```
e ruleset refuses # to encode, and queue.py:205 lacks the O_NOFOLLOW its siblings carry. The # rule correctly fires; --baseline-commit keeps main green as they predate # any diff. This locks the adjudication in as a finding, not a suppression. found = _scan(ARTIFACTS, QUEUE) > assert 143 in _lines(found, RULE_A, "artifacts.py") E AssertionError: assert 143 in {210} E + where {210} = _lines({('python-open-trunc-without-nlink-check', 'artifacts.py', 122), ('python-open-trunc-without-nlink-check', 'artifacts....python-open-without-nofollow-or-excl', 'artifacts.py', 210), ('python-open-without-nofollow-or-excl', 'queue.py', 205)}, 'python-open-without-nofollow-or-excl', 'artifacts.py') tests/test_semgrep_fsrules.py:137: AssertionError ================================ tests coverage ================================ _______________ coverage: platform linux, python 3.11.15-final-0 _______________ Coverage XML written to file coverage.xml =========================== short test summary info ============================ FAILED tests/test_semgrep_fsrules.py::test_rule_a_reports_the_two_unguarded_sites_as_findings 1 failed, 836 passed in 38.80s ------------- Diff Coverage Diff: 781cd2b69ffda5a2e1e6139559520b5ae3173985...HEAD, staged and unstaged changes ------------- orchestrator/verdict.py (100%) ------------- Total: 4 lines Missing: 0 lines Coverage: 100% ------------- @@GATE lint=0 types=0 tests=1 coverage=0 semgrep=0 gitleaks=0
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

`agent-error-visibility` is NOT merged and NOT deleted.
