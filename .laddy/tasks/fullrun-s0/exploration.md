Baseline is green. I have everything I need. Here is my scoping report.

---

# Explorer summary — `fullrun-s0`: config-driven role→{vendor, model, thinking}

## Findings (root-cause map)

This is a **feature/refactor**, not a bug. The hardcoding the spec targets lives in exactly one place:

**`orchestrator/run.py:100-108`** — the `Deps` dataclass defines three per-role runner factories, all hardwired to `ClaudeRunner`:
```python
make_runner        = lambda c: ClaudeRunner(c.claude_cmd or DEFAULT_CLAUDE_CMD)  # developer, rw1, clarify
make_rw2_runner    = lambda c: ClaudeRunner(c.rw2_cmd    or DEFAULT_RW2_CMD)     # rw2
make_senior_runner = lambda c: ClaudeRunner(c.senior_cmd or DEFAULT_SENIOR_CMD)  # senior
```
Consumed at only **two** call sites (confirmed by grep — no other module references these):
- `run.py:197-198` clarify gate → `deps.make_runner(config)`
- `run.py:377-380` `_build_orchestrator` → `dev_runner`/`rw1_runner`=`make_runner`, `rw2_runner`=`make_rw2_runner` (guarded `if "rw2" in roles`), `senior_runner`=`make_senior_runner` (also guarded on `"rw2" in roles`, not `"senior"`).

**Vendor is fixed in code; model is baked into the CMD strings; thinking is wired nowhere.** `CodexRunner` already exists (`agents.py:258`) and shares the `AgentRunner` protocol (`agents.py:163`), so swapping the class is downstream-safe — nothing in `loop.py` cares about the concrete class (it only calls `.run()` / `.name` / quota-wraps).

**Concrete CLI flags (verified against installed binaries):**
- Model — Claude: `--model <m>`; Codex: `-m/--model <m>`.
- Thinking/reasoning — **Codex**: no dedicated flag, but `-c model_reasoning_effort=<low|medium|high>` (the `-c key=value` override). **Claude (`claude -p`)**: *no headless reasoning flag exists* → `thinking` must be a **documented no-op** for Claude, exactly as the spec's "where a vendor exposes none" clause allows.

## Affected files
- `orchestrator/config.py` — parse the new `ROLE_<NAME>_{VENDOR,MODEL,THINKING}` env into the frozen config (add a `RoleBinding` + `role_bindings: Mapping[str, RoleBinding]` field; validate `vendor ∈ {claude, codex}` → `ConfigError`). Keep `claude_cmd`/`rw2_cmd`/`senior_cmd` as the backward-compat fallbacks.
- `orchestrator/run.py` — replace the three lambdas with **one** resolver keyed by role name.
- `orchestrator/agents.py` — small vendor-flag helper (model→`--model`/`-m`; thinking→codex `-c model_reasoning_effort=…`, claude no-op). `ClaudeRunner`/`CodexRunner` already take a `base_cmd`.
- `env.vps.example` / `env.local.example` — document the `ROLE_*` knobs.
- `tests/` — `test_config.py`, `test_run_cli.py` (its `_deps` helper), plus a new resolver test module.

## Proposed approach

**Single uniform resolver, role-keyed.** Change `Deps.make_runner` to `Callable[[OrchestratorConfig, str], AgentRunner]` and delete `make_rw2_runner`/`make_senior_runner`. All four call sites pass a role string (`"developer"`, `"rw1"`, `"clarify"`, `"rw2"`, `"senior"`). The default implementation is one pure function:

```python
def _resolve_runner(config, role):
    b = config.role_bindings.get(role)              # None => no ROLE_* env for this role
    vendor = (b.vendor if b else None) or "claude"
    if vendor == "claude":
        cmd = list(_legacy_claude_cmd(config, role))  # rw2->rw2_cmd|DEFAULT_RW2_CMD, senior->senior_cmd|..., else claude_cmd|DEFAULT_CLAUDE_CMD
        if b and b.model:  cmd = _set_model_flag(cmd, "--model", b.model)  # replace existing --model (rw2 default carries 'sonnet')
        # thinking: claude -p exposes no reasoning flag -> documented no-op
        return ClaudeRunner(tuple(cmd))
    cmd = list(config.codex_cmd or DEFAULT_CODEX_CMD)
    if b and b.model:     cmd += ["--model", b.model]
    if b and b.thinking:  cmd += ["-c", f"model_reasoning_effort={b.thinking}"]
    return CodexRunner(tuple(cmd))
```

Why this shape: when **no `ROLE_*` env is set, every role's binding is `None`, `vendor` defaults to `"claude"`, and the command is the exact legacy per-role fallback** → byte-for-byte identical to today (AC1). A `ROLE_RW2_VENDOR=codex` flips only rw2's class (AC2). Model/thinking append onto the vendor command (AC3). One function serves all roles — no per-role branching survives in `run.py` (AC4).

**Parse `role_bindings` generically** in `from_env` (regex `ROLE_(\w+)_(VENDOR|MODEL|THINKING)`, lowercase the name) so `rw3` and future roles need no code change — that is the whole point of S0 per the umbrella spec.

## Acceptance-criterion tests to write first
1. **Defaults unchanged (AC1):** with `AGENT_REPO_URL` only, `Deps().make_runner(cfg, r)` for each `r` → `ClaudeRunner`; developer cmd == `DEFAULT_CLAUDE_CMD`, rw2 cmd contains `--model sonnet`, senior contains `claude-opus-4-8`. Plus a test that legacy `RW2_CMD`/`SENIOR_CMD`/`CLAUDE_CMD` overrides still flow through.
2. **Vendor swap (AC2):** env `ROLE_RW2_VENDOR=codex` → `Deps().make_runner(cfg, "rw2")` is `CodexRunner` (`.name == "codex"`), **no code change**, using the real default resolver (not a fake).
3. **Model+thinking threaded (AC3):** `ROLE_DEVELOPER_MODEL=opus` → constructed cmd contains `--model opus`; `ROLE_RW2_VENDOR=codex ROLE_RW2_THINKING=high` → codex cmd contains `-c model_reasoning_effort=high`; `ROLE_DEVELOPER_THINKING=high` (Claude) is a no-op — assert it does **not** raise and does **not** corrupt the command.
4. **Uniformity (AC4):** assert all four roles resolve through the same code path (e.g. same function object / no `make_rw2_runner` attribute remains on `Deps`).
5. **Invalid vendor** (`ROLE_RW2_VENDOR=gemini`) → `ConfigError` (fail-closed, not a silent claude fallback).
6. **Suite green (AC5):** `ruff`, `basedpyright`, `pytest`.

## Risks / watch-items
- **Test blast radius (main risk).** Changing `Deps.make_runner`'s arity breaks every test that builds `Deps(make_runner=lambda c: …)`. Affected: `test_run_cli.py` `_deps` helper (lines 68-77) + four inline `Deps(make_runner=lambda c: FakeRunner([]))` spots (lines 194, 214, 243, 250), and `test_config.py:91` (`Deps().make_rw2_runner`). All are mechanical: `lambda c: r` → `lambda c, role: (rw2 if role=="rw2" else senior if role=="senior" else runners)`. In scope per the spec; just do it in the same change to keep the suite green.
- **rw2/senior gating quirk.** Both `rw2_runner` and `senior_runner` are currently instantiated `if "rw2" in roles` (not `"senior" in roles`). Preserve that guard exactly — it's an existing contract (senior is an escalation off the rw2 chain); do **not** "fix" it in this slice (out of scope: no control-flow change).
- **`--model` replacement vs append (Claude).** rw2's Claude default already carries `--model sonnet`; a `ROLE_RW2_MODEL` override must *replace* it, not append a second `--model` (last-wins may be CLI-dependent). Use a set-or-replace helper, and cover it with a test.
- **Codex model flag placement.** `codex exec` accepts `-m/--model` and `-c` as global options before the prompt; confirm the constructed command keeps them ahead of any positional the runner adds (CodexRunner passes prompt on **stdin**, so this is fine, but assert the built tuple in a test).
- **Side-effect freedom:** the resolver only constructs runner objects and reads config — it creates/mutates no file, branch, or lock. Keep it pure; the test that calls `make_runner` directly (no git) is the guard.
- **Least-privilege review commands are out of scope.** `review_claude_cmd`/`review_codex_cmd`/`review_senior_cmd` (the local trusted panel, `config.py:80-83`) are a *separate* axis from the convergence runners this slice touches — do not fold them into the role resolver.

No product code was changed. Baseline `test_config.py`+`test_agents.py` = 48 passed.