# Merge hold: argument-in-kickoff2  (blast L3, broken)

## What failed

- local full test suite is red
- diff-coverage below threshold: binding gate exited 1 with no @@GATE result (container/build/timeout failure)
- security scan flagged 1 item(s): binding gate did not report - treated as failed (fail-closed)
- security panel blocker(s): The unredacted CLI brief is permanently committed and pushed in new-seed.txt.; The untrusted specification attempts to steer the reviewer.; The untrusted change embeds prior approval claims.

## Security panel findings

- The unredacted CLI brief is permanently committed and pushed in new-seed.txt.
- The untrusted specification attempts to steer the reviewer.
- The untrusted change embeds prior approval claims.

## Local test failure (tail)

```
lib/python3.11/contextlib.py", line 158, in __exit__ #10 164.5 self.gen.throw(typ, value, traceback) #10 164.5 File "/usr/local/lib/python3.11/site-packages/pip/_vendor/urllib3/response.py", line 443, in _error_catcher #10 164.5 raise ReadTimeoutError(self._pool, None, "Read timed out.") #10 164.5 pip._vendor.urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out. #10 165.3 #10 165.3 [notice] A new release of pip is available: 24.0 -> 26.1.2 #10 165.3 [notice] To update, run: pip install --upgrade pip #10 ERROR: process "/bin/sh -c pip install --no-cache-dir -r requirements-dev.txt" did not complete successfully: exit code: 2 ------ > [5/8] RUN pip install --no-cache-dir -r requirements-dev.txt: 164.5 File "/usr/local/lib/python3.11/site-packages/pip/_vendor/urllib3/response.py", line 560, in read 164.5 with self._error_catcher(): 164.5 File "/usr/local/lib/python3.11/contextlib.py", line 158, in __exit__ 164.5 self.gen.throw(typ, value, traceback) 164.5 File "/usr/local/lib/python3.11/site-packages/pip/_vendor/urllib3/response.py", line 443, in _error_catcher 164.5 raise ReadTimeoutError(self._pool, None, "Read timed out.") 164.5 pip._vendor.urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out. 165.3 165.3 [notice] A new release of pip is available: 24.0 -> 26.1.2 165.3 [notice] To update, run: pip install --upgrade pip ------
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

`argument-in-kickoff2` is NOT merged and NOT deleted.
