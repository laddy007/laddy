I now have everything, all empirically validated against the pinned semgrep (1.169.0) and the real corpus. Here is the structured exploration summary.

---

# Exploration Summary — fullrun-s2: deterministic FS-safety semgrep rules

## Bottom line

Both dogfood classes are **mechanically expressible with zero false positives on the corpus** — I built and ran working rules against the real files with the pinned semgrep. **Rule C and Rule D are not-encodable** and must be documented, exactly as the spec predicts. The one non-obvious landmine (fixtures tripping the slice's own gate) turns out to be **already solved** by semgrep's default ignore of `tests/`. The two unguarded sites are **genuine low-severity findings**, not "safe by construction."

## Findings (all empirically verified)

**1. Rule A works and is precise.** `os.open($PATH, $FLAGS, ...)` + a `metavariable-regex` on `$FLAGS`:
```yaml
regex: '(?s)^(?=[\s\S]*O_(WRONLY|RDWR))(?![\s\S]*O_NOFOLLOW)(?![\s\S]*O_EXCL)'
```
Full-repo scan fires on **exactly** `artifacts.py:143` and `queue.py:205` — nowhere else. It correctly spares the canary `queue.py:104` (has `O_EXCL`), the read-only opens (`queue.py:156`; `report_path.py:83,86`), and the `O_NOFOLLOW` opens (`queue.py:177`; `report_path.py:112,145`). **AC2 passes.**

**2. Rule B's obvious formulation FAILS the canary — use the deep-expression operator.** The naive `pattern-not-inside: def…: … $X.st_nlink …` produced a **false positive on `report_path.py:129`**, because the guard lives inside an `if` *condition* (`if info.st_nlink != 1:`) and semgrep's `…` only matches `st_nlink` in *statement* position. The fix, verified working:
```yaml
- pattern: os.ftruncate($FD, 0)
- pattern-not-inside: |
    def $F(...):
        ...
        <... $X.st_nlink ...>
        ...
```
`<... … ...>` (deep expression operator) matches `st_nlink` at any depth. This correctly excludes `report_path.py:129` **and** still fires on a no-check fixture. This is the single most likely implementation mistake — flag it to the developer.

**3. AC8 answered empirically: `semgrep --error` blocks on WARNING too.** A WARNING-only finding returns rc=1. So severity (ERROR vs WARNING) does **not** change blocking — every finding blocks (the existing `python-weak-randomness-for-secrets` WARNING rule already blocks). Ship both new rules as `ERROR` (they represent genuine defects); record in the header that `--error` blocks regardless of severity.

**4. `--baseline-commit` behavior verified in a throwaway git repo.** A *new* rule firing on *unchanged* baseline code → **suppressed** (rc=0); the same pattern *in the diff* → **caught** (rc=1). So shipping Rule A does not break main even though it fires on the two pre-existing sites.

**5. CRITICAL RISK, already mitigated — fixtures under `tests/` do not trip the gate.** The gate scans the whole tree (`semgrep … --baseline-commit … .`), and fixture files are *new in the diff*, so naively they'd fail the slice's own gate. But semgrep applies a **built-in default `.semgrepignore` that excludes `tests/`** (verified: `tests/test_queue.py:254`, an identical unguarded pattern, is skipped by the `.` scan but found when targeted directly). Therefore:
   - Put anti-pattern fixtures under `tests/fixtures/semgrep/` → the gate skips them.
   - Per-rule tests invoke semgrep on the fixture **as an explicit target** (explicit targets override the ignore — verified) → the rule fires and the test asserts it.
   - **Do NOT add a repo-root `.semgrepignore`** — that would *replace* the default and could start scanning `tests/`. No `nosemgrep`, no tmp-materialization needed.

**6. AC3 adjudication of the two unguarded sites.** Both are flock **guard-file** opens whose fd is used *only* for `flock()` (never written through), on **engine-derived paths under `AGENT_WORK_ROOT`** (runtime state, never branch-committed; `task_id` is director/spec-derived). They are *plausibly* safe in practice. **But "the path is engine-derived" is a provenance/intent argument — exactly what the ruleset refuses to encode.** And `queue.py:205` lacks the `O_NOFOLLOW` its own siblings (`queue.py:156,177`) carry — an internal inconsistency. **Recommended call: report both as genuine low-severity findings, with this reasoning recorded** — not "safe," not `nosem`, not rule-narrowing (narrowing needs intent inference). `--baseline-commit` keeps main green, so they need not be *fixed* in this slice; recommend a follow-up hardening (add `O_NOFOLLOW`, ideally `O_CLOEXEC`). rw1/rw2 must ratify this.

## Affected files

- `security/semgrep.yml` **and** `.laddy/security/semgrep.yml` — add Rules A & B; add header note documenting Rules C/D as not-encoded and the AC8 severity finding. **Byte-identical today (verified `cmp`); keep so.**
- `tests/fixtures/semgrep/` (new) — anti-pattern fixtures (Rule A bad, Rule B bad).
- `tests/test_semgrep_fsrules.py` (new, name TBD) — the assertions below.

## Proposed approach / tests to write first

1. **Byte-identity (AC5):** `assert Path("security/semgrep.yml").read_bytes() == Path(".laddy/security/semgrep.yml").read_bytes()`. Runs everywhere; nothing keeps them in sync today.
2. **Offline/pinned + trust boundary (AC6/AC7):** grep-assert the ruleset has no registry/`--config auto`/network; assert `SEMGREP_CONFIG` and `_binding_gate` gate command unchanged; assert `trusted_ref=` passed only by `local_merge`. (Extend `tests/test_testgate.py` patterns.)
3. **Rule-behavior tests (AC1/AC2/AC3)** — guard with `@pytest.mark.skipif(shutil.which("semgrep") is None, …)`, mirroring the existing `requires_bash` idiom (host lacks semgrep; the gate container has `semgrep==1.169.0`, so these run in-container during `pytest`). Invoke `semgrep --json --config .laddy/security/semgrep.yml <target>` via subprocess and assert on results:
   - AC1: fixture with a param-path write-`os.open` (neither guard) → Rule A fires; fixture with `os.ftruncate(fd,0)` and no `st_nlink` → Rule B fires.
   - AC2: scan each real corpus file; assert **no** finding on `report_path.py:83,86,112,145`; `queue.py:104,156,177`.
   - AC3: assert Rule A **does** fire on `artifacts.py:143` and `queue.py:205` (locking in the adjudication as findings).
4. Add the two rules (Rule A regex form; Rule B deep-expression form) to both YAML copies + header note.

## Risks

- **Rule B canary regression** — anyone "simplifying" the deep-expression operator back to `… $X.st_nlink …` reintroduces the `report_path.py:129` false positive. The AC2 test on `report_path.py` catches it; keep that test.
- **Two-copy drift (AC5)** — nothing enforces sync but the new test. Edit both copies in the same change.
- **`skipif` masks the rule tests locally** — on the Director's box (no semgrep) the behavior tests SKIP (green but unexercised); they only really run inside the gate container. This matches the `requires_bash` precedent and is acceptable, but note it so a green local run isn't mistaken for full verification.
- **Don't add a repo `.semgrepignore`** (would replace the default and expose `tests/`); don't touch the gate command, `trusted_ref`, or make any VPS result authoritative (automatic reject per AC7).
- **`metavariable-regex` is name-based** — flags passed as a pre-computed variable (`flags = …; os.open(p, flags)`) won't match. That's a false *negative*, which the spec explicitly prefers over false positives; fixtures use inline flags.

*(Aside: I installed the pinned semgrep in `/tmp/sgvenv` for validation — no product code changed. The developer can reuse it: `/tmp/sgvenv/bin/semgrep`.)*