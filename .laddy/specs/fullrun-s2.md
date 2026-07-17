---
type: feature
roles: [developer, rw1, rw2]
risk: high
---
# fullrun-s2 — deterministic FS-safety rules in the offline semgrep ruleset

## Goal

Catch the filesystem-safety defect classes that produced the first two dogfood
merge holds **mechanically**, on both gate ends, so they fail fast and
deterministically instead of depending on an LLM noticing them.

This is slice **S2** of `.laddy/specs/fullrun.md` ("semgrep FS-safety rules on
both gate ends (deterministic, standalone)"). It is **standalone**: it depends on
no other slice and touches neither the loop, the verdict chain, nor rw3.

**This slice's real difficulty is precision, not coverage.** The gate *blocks
merges*. A rule that fires on correct code is worse than no rule: it blocks
legitimate work on the Director's own authority and trains everyone to reach for
an override. The Scope below is therefore deliberately narrower than the
umbrella's §3 wish-list, and the acceptance criteria are dominated by
false-positive tests against real, correct, already-merged code.

## Root-cause context

**1. semgrep already exists; this extends it.** `security/semgrep.yml` (mirrored
byte-identically at `.laddy/security/semgrep.yml`) is an offline, pinned ruleset
of six Python rules. Its header states the doctrine this slice must respect:

> Pinned + committed on purpose: `semgrep --config auto` fetches rules from the
> registry over the network, which (a) makes a "deterministic" gate depend on a
> remote and (b) is one more egress from the trusted machine while it processes
> untrusted branch code. […] Extend it as new defect classes show up; a change
> here is **L3**.
>
> Scope: the adversarial security lens from `.laddy/roles/security.md`, expressed
> as **mechanical patterns**. LLM panel covers what patterns cannot.

That last sentence is the boundary this slice works inside: **patterns for what is
mechanical, panel for the rest.** A rule that needs to guess intent is out of
scope by the ruleset's own design, not by omission.

**2. The two ends are not equally trusted, and that is deliberate.** The gate
command (`testgate.py:262-282`) is one shell line:

```
semgrep --error --config .laddy/security/semgrep.yml --baseline-commit <ref> .
```

Only `local_merge.py:522` passes `trusted_ref=base_sha`; the VPS `DockerGate`
does not. So `_containerized` (`testgate.py:212`) restores `.laddy/docker` and
`.laddy/security` from local trusted main **on the local end only**:

```
git -C "$tmp/repo" checkout <trusted_ref> -- .laddy/docker .laddy/security
```

The VPS therefore scans with **the branch's own copy of the rules** — an agent
could weaken them there. That is not a hole: the VPS gate is fast feedback, the
local re-derivation is authority, and the local end cannot be weakened because the
ruleset is restored from trusted main before the scan. This slice must not
"fix" the VPS end by making a VPS result authoritative.

**3. `--baseline-commit` scopes findings to the change.** New rules do **not**
retroactively fail existing code; only findings introduced by the diff count. So
adding rules cannot break main — but it also means the false-positive risk lands
on *future correct code*, which is exactly the code we cannot test in advance.
Hence the in-repo corpus below.

**4. The panel is a real reviewer — calibrate against it honestly.** On `mcp`
(a real diff) the security panel found three substantive things. On
`report-path-guard-md` it was dispatched against an **empty** review target and
produced two confident findings about attacks the code demonstrably defends
against — it had read the module's docstring, which describes the threat model,
and reported the threats as defects. The lesson is **not** "the panel is useless
and must be replaced by patterns"; it is that the panel degrades badly with no
diff (a separate bug, in `local_merge`'s dispatch). Deterministic rules are worth
having because they are *cheap and certain* on the classes patterns express
precisely — not because the panel cannot be trusted.

**5. The corpus: nine `os.open` sites in main, and they disagree.**

| Site | Flags | Guarded by |
|---|---|---|
| `report_path.py:83,86,112,145` | incl. `O_NOFOLLOW` | `O_NOFOLLOW` |
| `queue.py:156` | `O_RDONLY\|O_NOFOLLOW` | `O_NOFOLLOW` |
| `queue.py:177` | `O_CREAT\|O_WRONLY\|O_TRUNC\|O_NOFOLLOW` | `O_NOFOLLOW` |
| `queue.py:104` | `O_CREAT\|O_EXCL\|O_WRONLY` | **`O_EXCL`, no `O_NOFOLLOW`** |
| `artifacts.py:143` | `O_CREAT\|O_RDWR, 0o644` | **neither** |
| `queue.py:205` | `O_CREAT\|O_RDWR` | **neither** |

`queue.py:104` is the trap that kills the naive rule: `O_CREAT|O_EXCL` is
TOCTOU-free **without** `O_NOFOLLOW`, because a pre-existing symlink fails
`EEXIST` before any symlink is followed — precisely what
`report-path-guard-md`'s rw2 verified in its own verdict. A rule that says
"`os.open` must have `O_NOFOLLOW`" flags correct code.

The last two sites are the slice's genuine open question. Both are `flock`
lock-file opens on engine-derived paths (`locks / f"{task_id}.reclaim"`; the
artifacts path), so they are *plausibly* fine — but "plausibly" is not a finding.
**Determine which; do not assume.** Either they are safe and the rule must be
narrow enough to leave them alone, or the rule has just found a real defect in
main and that is a result worth having.

## Scope

**In:**

- `security/semgrep.yml` **and** `.laddy/security/semgrep.yml` — kept
  byte-identical (they are today; a drift between them means the VPS and local
  ends scan with different rules).
- New rules for the classes that are **mechanically expressible with zero
  false positives on the corpus** (see Behaviour).
- Test fixtures under `tests/` that (a) reintroduce each anti-pattern and assert
  the rule fires, and (b) assert the rule does **not** fire on the corpus.
- A short note in the ruleset header recording which of the umbrella's four
  classes were deliberately **not** encoded, and why (so the next reader does not
  assume they are covered).

**Out:**

- **No new dependency, no network.** `--config` stays this local file; nothing
  moves to `--config auto` or the registry.
- **No change to the gate command, the trust boundary, or which end is
  authoritative.** `trusted_ref` stays local-only; no VPS scan result becomes
  binding.
- **No rule that needs to infer intent or provenance** ("is this path
  attacker-controlled?"). That is the panel's half of the boundary the ruleset
  header draws. If a class cannot be expressed precisely, it is documented as
  not-encoded — **not** shipped noisy.
- Non-Python languages (the ruleset is Python-only today).
- The `local_merge` empty-review-target bug (see Root-cause 4) — real, but a
  different fix in a different module.
- rw3, the driver, the bundle (S1/S3/S5).

## Behaviour

Each rule below ships **only if** it satisfies the corpus test (AC2). A rule that
cannot be made precise is **documented as not-encoded** and left to the panel —
that is a valid, expected outcome of this slice, not a failure.

**Rule A — unguarded write-open (`python-open-without-nofollow-or-excl`).**
Flag an `os.open(...)` opened for **writing** whose flags contain **neither**
`O_NOFOLLOW` **nor** `O_EXCL`. Both are independently sufficient: `O_NOFOLLOW`
refuses to follow a final-component symlink; `O_CREAT|O_EXCL` fails `EEXIST`
against one. Requiring both would flag `queue.py:104`; requiring only
`O_NOFOLLOW` would too. Read-only opens are out (the risk is the write).

**Rule B — force-overwrite without a hard-link check
(`python-ftruncate-without-nlink-check`).** Flag an `os.ftruncate($FD, 0)` whose
enclosing function contains no `st_nlink` test. A hard link to a sensitive file
*is* a regular, non-symlink file, so `O_NOFOLLOW` + `S_ISREG` pass and the
truncate lands on the linked file — the exact `--force` hole `report_path.py`
closes at line 124. Expressible with `pattern-inside` / `pattern-not-inside`;
scope it to the enclosing function, not the file.

**Rule C — parent-directory TOCTOU (realpath-authorise, reopen-by-string).**
The umbrella asks for this. It is a **dataflow-and-ordering** property: "a path
authorised via `realpath` is later re-opened by string rather than relative to a
pinned dir-fd". Attempt it with semgrep OSS taint mode
(`os.path.realpath` → `os.open`). **If it cannot be encoded without firing on
`report_path.py` — which does exactly this shape, correctly, because the realpath
only informs the confinement decision while the write walks from a root dir-fd —
do not ship it.** Document it as not-encoded and say why. This is the most likely
rule to be dropped, and dropping it is the right call.

**Rule D — write outside a validated root from agent-supplied input.** Pure taint
analysis over provenance. Almost certainly not precisely expressible in semgrep
OSS; the expected outcome is not-encoded + documented. Do not force it.

Severity and blocking: determine empirically whether `semgrep --error` fails the
gate on `WARNING`-severity findings or only on `ERROR` — the existing ruleset
already carries one `WARNING` rule (`python-weak-randomness-for-secrets`), so the
answer decides whether a new rule blocks or merely reports. State the answer in
the ruleset header; do not guess it.

## Acceptance criteria

1. **Each shipped rule catches its anti-pattern.** A fixture reintroducing the
   pattern makes the rule fire — asserted per rule:
   - Rule A: a write-`os.open` on a parameter-supplied path with neither
     `O_NOFOLLOW` nor `O_EXCL`.
   - Rule B: an `os.ftruncate($FD, 0)` in a function with no `st_nlink` test.
   These are the two dogfood classes from `fullrun.md` AC4
   (parent-symlink TOCTOU; force-mode hard-link overwrite).
2. **Zero false positives on the in-repo corpus.** No shipped rule fires on any
   of: `report_path.py:83,86,112,145`; `queue.py:104` (the `O_EXCL`-only site);
   `queue.py:156,177`. Each is correct, in main, and reviewed. Asserted site by
   site — this criterion outranks AC1: a rule that cannot pass it is dropped, not
   loosened.
3. **The two unguarded sites are adjudicated, not skipped.**
   `artifacts.py:143` and `queue.py:205` open with neither guard. The task must
   determine whether each is safe (engine-derived path → rule narrowed to leave it
   alone) or a genuine defect (→ reported as a finding, with the reasoning). A
   silent exclusion, a blanket `nosem`, or an unexamined pass is a reject.
   Whichever way it lands, the reasoning is recorded.
4. **Not-encoded classes are documented, not implied.** For every class in
   `fullrun.md` §3 not shipped as a rule (expected: C and D), the ruleset header
   names it and says why, so a later reader does not assume coverage. A silent
   omission is a reject.
5. **The two copies stay identical.** `security/semgrep.yml` and
   `.laddy/security/semgrep.yml` are byte-identical after the change — asserted by
   a test, since a drift means the VPS and local ends scan with different rules.
6. **Offline and pinned.** The ruleset remains the sole `--config`; no rule pulls
   from the registry; the gate command is unchanged. Grep-asserted.
7. **Trust boundary untouched.** `trusted_ref` is still passed only by
   `local_merge` (the local end restores `.laddy/security` from trusted main); no
   VPS scan result becomes authoritative; the gate command's sentinel/exit
   semantics are unchanged. Grep/test-asserted.
8. **Severity behaviour is stated, not assumed.** The ruleset header records
   whether `semgrep --error` blocks on `WARNING`, established by an actual run,
   and each new rule's severity is a deliberate consequence of that answer.
9. Suite green: `pytest -n auto -q`, `ruff check .` clean, `basedpyright` clean
   for the touched scope.

## Notes for the reviewer

- **AC2 outranks AC1 — enforce that ordering.** The instinct is to reward
  coverage. Here the gate blocks merges, so a rule that fires on correct code
  costs more than a missing rule: it blocks legitimate work under the Director's
  own authority and teaches everyone to override the gate. If a rule cannot be
  both precise and useful, the right outcome is **no rule + a documented reason**.
  Approve a slice that ships two solid rules and documents two it could not
  express. Reject one that ships four noisy ones.
- **`queue.py:104` is the canary.** `O_CREAT|O_EXCL` without `O_NOFOLLOW` is
  correct. If Rule A fires there, the rule is wrong — not the code. Check this
  first; it is the single most likely implementation error.
- **`report_path.py` is the canary for Rule C.** It uses `realpath` to authorise
  and then opens — correctly, because the write walks from a pinned root dir-fd
  and never re-traverses the string. A taint rule naive about that will flag the
  best FS-safety code in the repo. That outcome means "drop Rule C", not "add a
  `nosem`".
- **Do not let AC3 be answered by a suppression.** "Add `# nosemgrep` and move on"
  is the failure mode. Either the site is safe for a *reason that can be stated*
  (and the rule should be narrow enough not to reach it), or it is a finding.
- **Watch the two-copy invariant (AC5).** Nothing in the code keeps
  `security/semgrep.yml` and `.laddy/security/semgrep.yml` in sync today; they are
  identical only by hand. Adding rules to one and not the other silently gives the
  two gate ends different rules — the local one wins (it restores from trusted
  main), so the drift would show up as "the VPS passed and local failed" with no
  obvious cause.
- **Do not widen the trust surface.** The temptation, seeing that the VPS scans
  with the branch's own rules, is to "fix" it. Resist: the VPS is untrusted by
  design and the local re-derivation is the anchor. Passing `trusted_ref` on the
  VPS end would be meaningless (there is no trusted ref there); making a VPS scan
  binding would be a trust regression and an automatic reject.
- Reject if: any rule fires on the corpus (AC2); a not-encoded class is silently
  omitted (AC4); the two copies drift (AC5); a `nosem`/suppression stands in for
  adjudicating AC3; the ruleset gains a network fetch; or any VPS scan result is
  made authoritative.
