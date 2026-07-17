---
type: feature
roles: [developer, rw1, rw2]
risk: medium
---
# agent-error-visibility — a failed agent run reports what the agent actually said

## Goal
When a headless agent run fails, the reason **the agent itself gave** must reach
the human-facing report. Today `request_payload` (`orchestrator/verdict.py`)
discards `AgentResult.text` on any non-`"ok"` exit and builds its error from
`exit_reason` and `returncode` alone, so every failure mode looks identical: an
expired login, a `--model` flag the installed CLI rejects, a crashed CLI and a
timed-out one all surface as the same sentence. The engine holds the answer and
throws it away.

## Root-cause context
The root cause is **already established — this task needs no exploration.**

Real incident: the local security panel abstained on every L3 branch with
`security panel member 'claude' did not return a valid verdict ... agent run did
not complete cleanly (exit_reason='error', rc=1)`. That was as much as the engine
would say. The actual cause could only be found by running the panel's exact
command by hand:

```
{"is_error":true, ..., "result":"Failed to authenticate: OAuth session expired
and could not be refreshed"}
```

The chain, all of which already exists:

1. `ClaudeRunner.run` (`orchestrator/agents.py`) parses that envelope and puts
   the sentence in `AgentResult.text` — the information is present and typed.
2. `request_payload` (`orchestrator/verdict.py`) sees `exit_reason != "ok"`,
   composes `last_error` from `exit_reason` + `returncode`, and **never
   references `result.text` again.** This is the whole defect.
3. `_abstention_blocker` (`orchestrator/local_merge.py`) already carries
   `last_error` into the blocker summary, bounded by
   `_ABSTENTION_REASON_MAX = 300`.

So step 3 is already wired: the moment step 2 includes the agent's text, it
reaches the digest end to end. That existing 300-char bound is the budget this
change lives inside — see AC2, which is the criterion that actually matters.

## Scope
**In:** `request_payload` in `orchestrator/verdict.py` — include a bounded,
whitespace-collapsed snippet of the failed run's `AgentResult.text` in
`last_error`; tests under `tests/`.

**Out:** the retry policy (an auth failure still consumes its retries — a real
but separate concern; do not couple it here); any new `exit_reason` value;
`detect_quota` and quota handling; `_ABSTENTION_REASON_MAX` and anything else in
`orchestrator/local_merge.py`; `ClaudeRunner` / `CodexRunner` behaviour; the
`--model` flag and CLI authentication itself (an operator concern, not code).

## Behaviour
On a non-`"ok"` run, `last_error` names the exit reason, the return code, **and
what the agent said**:

```
agent run did not complete cleanly (exit_reason='error', rc=1): Failed to
authenticate: OAuth session expired and could not be refreshed
```

- The text is **agent-controlled** and lands in a report a human reads. Bound the
  snippet and collapse whitespace so a runaway or multi-line blob cannot break
  the digest's structure or bury the rest of the report.
- Empty text (a runner that failed silently) must read cleanly — no dangling
  separator or punctuation; the message still names `exit_reason` and `rc`.
- The fail-closed contract is **unchanged**: the text is quoted as a
  **diagnostic**, never parsed, and a non-`"ok"` run's output still never reaches
  `parse`. This change makes a failure legible, it does not make it trusted.

## Acceptance criteria
1. **The agent's words survive into the error.** A runner whose run returns
   `exit_reason="error"` and a known sentence produces a `VerdictError` whose
   message contains that sentence. Asserted with a fake runner via the public
   `request_verdict` / `request_payload`.
2. **End-to-end: the sentence reaches the panel blocker.** A panel member whose
   run fails with `Failed to authenticate: OAuth session expired and could not be
   refreshed` yields a `run_security_panel` blocker whose **summary contains that
   sentence**. This is the load-bearing criterion: a fix that is truncated away by
   `_ABSTENTION_REASON_MAX` before a human can read it has changed nothing.
   Asserted through the public `run_security_panel`.
3. **Bounded.** A runner returning a very large text (e.g. 10k chars) still yields
   a `last_error` under an explicit, named limit — asserted on length, not on
   eyeballing.
4. **Whitespace collapsed.** A multi-line agent text does not inject raw newlines
   into `last_error`; the resulting digest section stays one readable item.
5. **Empty text reads cleanly.** A failed run with no text produces a message that
   still names `exit_reason` and `rc` and carries no dangling separator — asserted
   on the exact string, not merely "does not crash".
6. **Never parsed.** A non-`"ok"` run whose text happens to be a schema-valid
   verdict JSON is still not accepted: it consumes a retry and its content never
   becomes a verdict. The existing fail-closed tests stay green, unmodified.
7. **Quota path unchanged.** A `exit_reason="quota"` run behaves exactly as today
   apart from the added text; `detect_quota` and `QuotaAwareRunner` are untouched.
8. Suite green for the touched scope: `ruff check .` clean, `basedpyright` at 0
   errors, `pytest -n auto -q` green.

## Notes
- **This is plumbing, not policy.** The temptation is to also fix the neighbouring
  defect — that an auth failure is retried twice before it abstains, when no retry
  can ever fix an expired login. That is real and worth doing, but it changes
  *control flow* (a new non-retryable condition) where this change only makes an
  existing failure legible. Keep them apart; a follow-up slice can classify
  non-retryable failures once this lands.
- **Do not widen the trust surface to do it.** Including the text must not tempt
  anyone into parsing it, checking it for a verdict, or branching on its content.
  It is quoted for a human and nothing else. `request_payload`'s docstring already
  states why a failed run's output is never authoritative — that reasoning stands
  and the docstring should end up saying both things.
- `request_payload` is shared by every consumer of untrusted agent output
  (reviewer verdicts, the investigator payload, the explorer text), so the
  improvement lands for all of them at once. Check that the investigator/explorer
  callers' existing tests still pass rather than assuming the blast radius is the
  panel only.
- The bound in `local_merge._ABSTENTION_REASON_MAX` is 300 and the existing prefix
  already spends roughly 130 of it. Choose this change's own limit so a realistic
  agent error (the authentication sentence above is 71 chars) survives AC2 rather
  than being clipped at the boundary. If the two bounds cannot both be honoured,
  say so in the report instead of quietly widening the one that is out of scope.
