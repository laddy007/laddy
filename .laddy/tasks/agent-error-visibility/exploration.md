I now have everything needed to scope this. No product code changed (read/analyze only).

---

# Explorer summary — `agent-error-visibility`

## Findings

**The defect is exactly where the spec says.** `orchestrator/verdict.py:289-296`, inside `request_payload`, is the whole bug:

```python
if result.exit_reason != "ok":
    last_error = (
        f"agent run did not complete cleanly "
        f"(exit_reason={result.exit_reason!r}, rc={result.returncode}); "
        "its output is not trustworthy"
    )
    attempt_prompt = RETRY_TEMPLATE.format(original=prompt, error=last_error)
    continue
```

`result.text` (which `ClaudeRunner.run` already populates from the `"result"` field of the JSON envelope — `orchestrator/agents.py:263,266`) is never referenced. Every non-`"ok"` failure collapses to one indistinguishable sentence.

**The end-to-end wiring downstream already exists and is unmodified by this task:**
- `request_payload` final raise (`verdict.py:302`): `VerdictError(f"output still malformed after {max_retries} retries: {last_error}")` — embeds `last_error`.
- `run_security_panel` (`local_merge.py:236-240`) catches that `VerdictError` and passes `str(exc)` to `_abstention_blocker`.
- `_abstention_blocker` (`local_merge.py:251-279`) puts it in the blocker `summary`, truncating to `_ABSTENTION_REASON_MAX = 300` (+`"..."`). Out of scope; do not touch.

So the moment step 2 includes `result.text`, it flows to the human-facing digest.

**Budget math (AC2 is load-bearing) — verified by computation, both bounds are honourable:**

| Segment | Chars |
|---|---|
| `"output still malformed after 2 retries: "` (final-raise wrapper) | 40 |
| `"agent run did not complete cleanly (exit_reason='error', rc=1): "` (new prefix) | 64 |
| **Prefix before the snippet** | **104** |
| The auth sentence | 72 |
| **Total reaching the 300-char abstention bound** | **176** ✓ well under 300 |

Room for the snippet inside the 300 bound is **196 chars**. So a snippet limit **≤ 196** honours *both* this change's own bound and the out-of-scope `_ABSTENTION_REASON_MAX=300` with no double-truncation. **The two bounds are compatible** — no need to widen the out-of-scope one, and nothing to report as a conflict.

## Affected files
- **`orchestrator/verdict.py`** — `request_payload` non-`"ok"` branch (`~289-296`); its docstring (`268-282`). *Only file with a product change.*
- **`tests/test_verdict.py`** and **`tests/test_local_merge.py`** — new tests (existing patterns at `test_verdict.py:146` and `test_local_merge.py:228,241` are near-exact templates).

## Proposed approach

Add a named module constant and a whitespace-collapse helper in `verdict.py`, then rebuild `last_error` in the non-`"ok"` branch:

```python
_ERROR_TEXT_MAX = 180  # ≤196 keeps both this bound and local_merge's 300 honoured

# in the non-ok branch:
base = (f"agent run did not complete cleanly "
        f"(exit_reason={result.exit_reason!r}, rc={result.returncode})")
snippet = " ".join(result.text.split())[:_ERROR_TEXT_MAX]  # collapse ws + bound
last_error = f"{base}: {snippet}" if snippet else f"{base}; its output is not trustworthy"
```

- `" ".join(text.split())` collapses every whitespace run (incl. newlines) to a single space and yields `""` for empty/whitespace-only text (AC4, AC5).
- Empty snippet keeps the existing trailing clause → no dangling `":"`/separator (AC5). This exact-string decision is the one judgement call — see risks.
- `_ERROR_TEXT_MAX = 180` is recommended: max total reason = 284 ≤ 300 (no double-truncation), realistic 72-char errors survive with comfortable headroom. **190** is the ceiling that still fits; **200+** would let a maximal snippet get re-clipped by the abstention bound (only for pathologically long text — realistic errors survive regardless).
- Extend the docstring to state *both* things per Notes: the text is quoted as a **diagnostic only, never parsed / never trusted**, and a non-`"ok"` run still never reaches `parse`.

## Acceptance-criterion tests to write first
1. **AC1** (`test_verdict.py`): `FakeRunner` queuing `AgentResult(text="Failed to authenticate: OAuth session expired and could not be refreshed", session_id=None, exit_reason="error", returncode=1)` ×3 → `request_verdict` raises `VerdictError` whose message contains the sentence.
2. **AC2** (`test_local_merge.py`, mirrors `test_panel_abstention_carries_the_reason_it_abstained:228`): panel member queuing that errored `AgentResult` ×3 → a `run_security_panel` blocker whose **`summary`** contains the full sentence (the load-bearing end-to-end assertion — not just `last_error`).
3. **AC3** (bounded): runner returning `text="x"*10000, exit_reason="error"` → assert `len` of the snippet region against `_ERROR_TEXT_MAX` (named constant, not eyeballed).
4. **AC4** (whitespace): `text="line1\n\n  line2\tline3"` → resulting message contains no `\n`; collapsed to single spaces.
5. **AC5** (empty): `text="", exit_reason="error", returncode=1` → assert the **exact** string `"...(exit_reason='error', rc=1); its output is not trustworthy"`, no dangling separator.
6. **AC6** (never parsed): already covered green by `test_trust_blockers.py:110` (`test_non_ok_run_verdict_refused`) — its `_FakeRunner` returns a **schema-valid `_APPROVED` JSON as the errored text**; still raises `VerdictError`. Leave unmodified; confirm it stays green (the valid JSON now appears bounded in `last_error` but is never parsed).
7. **AC7** (quota): `exit_reason="quota"` run behaves as today plus the text; `detect_quota`/`QuotaAwareRunner` untouched.

## Risks
- **Blast radius is all four consumers, not just the panel.** `request_payload` is shared by the explorer (`loop.py:960`, `parse=lambda t: t`) and investigator (`loop.py:994`) too. Their existing tests (`test_loop_report_only.py:405,428`) assert only on outcomes/artifacts, not on the error string, so they stay green — but run the full suite, don't assume panel-only.
- **The exact empty-text string is the one design choice.** Recommendation: keep the trailing `"; its output is not trustworthy"` for the empty case (preserves existing wording, satisfies AC5's "names exit_reason and rc, no dangling separator"). AC5 asserts an exact string, so the developer must pin whichever they choose. Grep confirms **no existing test asserts on `"not trustworthy"`**, so dropping that clause in the *with-text* case is safe.
- **Agent-controlled text now re-enters the retry prompt** (`RETRY_TEMPLATE.format(... error=last_error)`). This is not new — the parse-error path already echoes agent-rejected content — and it is bounded/collapsed here, so no new trust surface. Do **not** be tempted to parse, validate, or branch on the snippet (spec Notes).
- **Scope creep trap** (spec Notes): the retry-an-unfixable-auth-failure defect is real but out of scope — it changes control flow; this change only makes an existing failure legible. Keep them apart.
- **Clock/injection & LF/ASCII invariants**: no time involved here; the auth sentence and all literals are ASCII. No concern.

No blockers found; the two bounds are compatible, so nothing needs to be escalated per spec note 8.