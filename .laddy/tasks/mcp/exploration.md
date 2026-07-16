I now have a complete picture. No code was changed (read/run/analyze only). Here is the structured exploration summary.

---

# Explorer summary — `mcp` (TOTP-gated `save_note` MCP server)

## Verdict
This is a **greenfield feature**, not a bug hunt — there is no existing `note_server/`, no `mcp` dependency, and no prior TOTP/MCP code anywhere in the tree. The spec is unusually precise, so the real risk is **not** the server logic (which is straightforward) — it's the **repo's authoritative merge gate**, which does far more than the spec's AC8 admits (`ruff` + `basedpyright` + `pytest` + **diff-cover + semgrep + gitleaks**). Three of those interact badly with this task's requirements unless handled deliberately. Those are the findings below.

## Key facts established
- **MCP SDK API** (official `mcp` package, `mcp.server.fastmcp.FastMCP`): register with `@mcp.tool()`; a signature `def save_note(token: str, project_name: str, content: str) -> str` auto-generates an input schema with all three required string params (satisfies AC1). Bind host/port are constructor settings: `FastMCP(name, host=..., port=...)`; start with `mcp.run(transport="streamable-http")`. **Default Streamable-HTTP mount path is `/mcp`** → the public address is `https://<vps-host>:8443/mcp` (proxy → internal bind). ([running-server docs](https://gofastmcp.com/deployment/running-server), [python-sdk](https://github.com/modelcontextprotocol/python-sdk))
- **The base32 secret decodes cleanly.** `KNVWKZLWFVHWW2LOMF3WC` (len 21) → pad to 24 with `===` → `base64.b32decode` → **13 bytes** (ASCII `Skeev-Okinawa`). Reference TOTP vectors (HMAC-SHA1, 6 digits, 30s) I computed for the developer to assert against:
  - `t=0` (counter 0) → **`411400`**
  - `t=59` (counter 1) → **`795119`**
  - `t=1234567890` (counter 41152263) → **`948172`**

## Affected files (proposed)
| File | Change |
|---|---|
| `note_server/__init__.py`, `config.py`, `totp.py`, `writer.py`, `server.py` | new package (see approach) |
| `pyproject.toml` | add a `[project]` table with `dependencies = ["mcp"]`; **add `"note_server"` to `[tool.basedpyright].include`** (currently `["orchestrator", "tests"]` — AC8 requires it) |
| `requirements-dev.txt` | **add `mcp`** — see Finding 1 (mandatory, not optional) |
| `tests/note_server/test_*.py` | unit tests per AC2–AC7 (mirror `tests/loop_monitor/`: no `__init__.py` needed, optional `conftest.py`) |
| a run/deploy note (e.g. `note_server/README.md`) | env vars + "TLS proxy terminates HTTPS on 8443 → forwards to the plain-HTTP bind at `/mcp`" |

## Proposed approach (keep everything pure/injectable, thin transport wrapper)
Split so each piece is unit-testable **without standing up HTTP**:
- `decode_secret(b32) -> bytes` (upper-case + pad to mult. of 8).
- `totp(key, timestamp, *, step=30, digits=6) -> str`.
- `verify(token, key, *, now: float, drift=1) -> bool` — compares `token` against counters `c-1, c, c+1` (use `hmac.compare_digest`). **`now` is injected** (AC2).
- `validate_project_name(name) -> bool` — `re.fullmatch(r"[A-Za-z0-9_-]+", name)`.
- `write_note(folder, project_name, content) -> str` — loop `os.open(path, O_CREAT|O_EXCL|O_WRONLY)`, on `FileExistsError` try `{name}-2.md`, `-3.md`, …; return the basename actually created (AC5, TOCTOU-free).
- `NoteConfig.from_env(env)` — folder (**must exist**), host, port (**all required, no default**; port parsed as int) → raise a typed `ConfigError` on any missing/bad value (AC7). Mirror the existing `orchestrator/config.py` `from_env` + `ConfigError` idiom.
- `handle_save_note(cfg, token, project_name, content, *, now) -> str` — orchestrates auth → validate → write; the `@mcp.tool()` function is a thin wrapper calling this with `now=time.time()`.

### Behavioural contracts to write as tests (the reviewer-miss classes)
1. **Auth-before-write ordering & side-effect freedom:** on bad token / bad `project_name`, assert the folder is **still empty** (no file, no partial). Every reject path must create *nothing*.
2. **Allowlist, not denylist:** the regex is the authoritative set — test the accept case *and each* reject: `""`, `foo.md`, `a/b`, `../x`, `"a b"` (whitespace). Explicit traversal cases required (AC3).
3. **Drift window is exactly ±1:** counter `c-2` and `c+2` **rejected**; `c-1/c/c+1` accepted. Use a **realistic fixed `now`** (e.g. `1_000_000_000`), not `now=0` — see Risk 4.
4. **No-clobber leaves the original untouched:** pre-plant `{name}.md` with sentinel content, call, assert new file is `{name}-2.md` and the original bytes are unchanged (AC5).
5. **Path never leaks:** assert the configured folder's absolute-path substring is **absent** from every auth/validation/write error string (AC6).
6. **Config fails loud:** missing folder, non-existent folder, missing port each raise at startup (AC7).

## Risks / gate interactions (the important part)

**1. `mcp` will NOT be installed in the authoritative gate — declaring it in `pyproject.toml` alone is insufficient.** `.laddy/docker/Dockerfile.test` (the real gate image for this repo) installs **only** `requirements-dev.txt` + pinned scanners, then `COPY . .` — it never runs `pip install .`, so `[project].dependencies` is dead at gate time. Any test that imports `mcp` (or `note_server`, which imports `mcp`) → `ModuleNotFoundError` → **whole gate fails**. **Fix: add `mcp` to `requirements-dev.txt`** (and pin it, matching the Dockerfile's deliberate-pinning convention) in addition to the pyproject declaration the spec asks for. The fast local gate uses a `.venv` (`DEFAULT_FAST_COMMANDS`) the developer bootstraps — `mcp` must be `pip install`ed there too; putting it in `requirements-dev.txt` covers both.

**2. gitleaks may reject the hardcoded secret.** The merge gate runs `gitleaks detect --log-opts=origin/main..HEAD` with **no repo config → default ruleset**, which includes `generic-api-key` (keyword `secret`/`key` + high-entropy value). The spec *requires* committing the base32 secret `KNVWKZLWFVHWW2LOMF3WC`, which is exactly the shape that rule flags. If it fires, the deterministic gate blocks the merge. **Mitigation: an inline `# gitleaks:allow` on the secret line** (cheapest; keeps the secret literal per spec) **or a `.gitleaks.toml` allowlist.** The developer should verify with `gitleaks` locally before handing off. This is the single most likely surprise failure.

**3. Coverage is measured against `orchestrator`, so `note_server` coverage is *not* enforced by the gate — do not try to "fix" that.** `.laddy/policy.toml` sets `coverage_package = "orchestrator"`; the gate runs `pytest --cov=orchestrator`, so `note_server` lines never enter `coverage.xml` and `diff-cover --fail-under=90` simply ignores them (passes vacuously). That's fine and expected — the spec's "covered" is satisfied by the tests existing, *not* by diff-cover. **Do NOT edit `policy.toml`/`coverage_package` to include `note_server`**: that file is engine-sensitive and L3 human-gated (`security_globs`), and changing it would trip the trust gate. Leave coverage_package alone.

**4. `struct.pack(">Q", counter)` raises on a negative counter.** If a test injects a tiny `now` (e.g. `0`), the `c-1` neighbour is `-1` and packing an unsigned 64-bit from `-1` throws — a real crash class, not a logic bug. Guard (`skip counters < 0`) *or* only use realistic timestamps in tests. Flagging so the developer picks a large fixed `now`.

**5. semgrep is clean for this code (low risk).** The offline ruleset (`security/semgrep.yml`, `--error`) only flags `eval/exec`, `shell=True`, unsafe deserialization, `verify=False`, SQL string-building, weak randomness. The server uses `hmac/hashlib/base64/struct/os.open` — none match. No action needed, just don't introduce those patterns.

**6. Minor:** adding a `[project]` table means also giving it `name`/`version`/`requires-python = ">=3.11"` so packaging tools don't complain; base image is `python:3.11-slim`, so target py3.11 (matches existing `ruff`/`basedpyright` config). Return errors from the tool as plain strings (successful MCP result with error text) rather than raising — simplest to unit-test and satisfies "clear error naming which check failed."

Sources: [FastMCP running-server](https://gofastmcp.com/deployment/running-server), [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk).