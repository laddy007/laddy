---
type: feature
roles: [developer, rw1, rw2]
risk: high
---
# mcp — a TOTP-gated MCP server exposing a single `save_note` tool

## Goal
Build a small, self-contained MCP server that runs on the VPS and exposes exactly
one tool, `save_note`, which writes a caller-supplied note to a `.md` file in one
fixed, server-side folder. Every call is authenticated with a TOTP code (RFC 6238)
before anything is written. The design is deliberately single-user: one hardcoded
shared secret, one shared output folder. It is network-facing and handles
untrusted input (`project_name` becomes a filename, `token` gates writes), so the
security boundaries — auth-before-write and a path-traversal guard on
`project_name` — are the core of the task, not an afterthought.

## Tool contract
- **Name:** `save_note`
- **Parameters** (all required, all type `string`):
  - `token` — the caller's current TOTP code.
  - `project_name` — base name for the note file (see validation below).
  - `content` — the note body, written verbatim to the file.
- **Success response:** a confirmation message plus the **exact final filename
  used** (which may differ from `{project_name}.md` if it was de-duplicated).
- **Failure response:** a clear error naming *which* check failed (bad token /
  invalid `project_name` / write error), and **never** the folder's absolute
  filesystem path.

## Design decisions (agreed with Director)
1. **MCP layer:** use the official **MCP Python SDK** with its Streamable-HTTP
   transport. This introduces the repo's **first runtime dependency** — declare
   `mcp` in `pyproject.toml` (add a `[project]` `dependencies` list) and keep it
   the only runtime dep added.
2. **TLS / transport:** the server binds **plain HTTP** (default `localhost`); an
   external reverse proxy terminates TLS and presents the public **HTTPS endpoint
   on port 8443**. The server does **not** load certs or terminate TLS itself.
   The `8443` HTTPS contract is documented as the proxy's job; it is out of scope
   for the server code.
3. **Configuration:** the fixed notes folder and the bind host/port are read from
   **environment variables**, all **required with no default** — startup fails
   clearly and non-silently if the notes folder is unset/missing or if the bind
   host/port is unset. The TOTP shared secret stays **hardcoded in the server
   source** exactly as specified — it is *not* an env var.
4. **Endpoint URL path:** not fixed by this spec — the server uses the MCP SDK's
   default Streamable-HTTP route. So the address the agent calls is
   `https://<vps-host>:8443<SDK-default-path>` (proxy → internal bind). Whatever
   path the SDK defaults to is authoritative; document the resolved path in the
   run/deploy note once implemented.

## Authentication (TOTP)
- Standard RFC 6238: **HMAC-SHA1, 6 digits, 30-second time step**.
- Shared secret, base32, hardcoded in the server source:
  `KNVWKZLWFVHWW2LOMF3WC`. Decode it with correct base32 handling (upper-case,
  pad to a multiple of 8) before use.
- On each request the server computes the valid code(s) server-side and accepts
  the received `token` if it matches the current window **or ±1 step** (clock
  drift). Anything else (including ±2) is an auth failure.
- On an auth failure the server **writes no file** and returns the auth error.
- Implement TOTP with the standard library (`hmac`, `hashlib`, `base64`,
  `struct`, `time`) — no extra dependency. The current time must be **injectable**
  (e.g. a clock/`now` parameter) so the window logic is deterministically testable.

## Filename handling
- Validate `project_name` against `^[A-Za-z0-9_-]+$`. Reject (write nothing) if it
  fails — this is the path-traversal guard: **no** slashes, dots, `..`,
  whitespace, or any other character, and not empty.
- Final filename is `{project_name}.md`, written into the one configured folder
  (the folder path is server-side config, never part of the payload).
- **No overwrite.** If `{project_name}.md` already exists, auto-append a
  disambiguator to produce a unique name. Use a **deterministic numeric suffix** —
  `{project_name}-2.md`, `{project_name}-3.md`, … — created race-free with
  `O_CREAT | O_EXCL` (retry on collision), so the operation is TOCTOU-free and
  unit-testable. Return the exact name actually created.
- Defence in depth: even though the regex already forbids separators, confirm the
  resolved write path stays inside the configured folder before creating the file.

## Scope
**In:** a new self-contained server package (e.g. `note_server/`) with the MCP
`save_note` tool, the TOTP verifier, the `project_name` guard + no-clobber
writer, env-var config loading, an entrypoint that starts the SDK's
Streamable-HTTP server on the configured host/port, the `mcp` dependency in
`pyproject.toml`, a short run/deploy note (env vars + "put a TLS proxy in front on
8443"), and tests under `tests/` (e.g. `tests/note_server/`).

**Out:** TLS termination / certificate handling (reverse proxy's job); any
per-user secrets or per-user/per-project folders (see Known limitation); any
second tool or any read/list/delete capability; changes to the orchestrator, the
dev-loop, or the merge/trust path; any runtime dependency beyond `mcp`.

## Acceptance criteria
1. **Tool surface.** The server registers exactly one tool, `save_note`, with the
   three required string parameters `token`, `project_name`, `content`;
   `tools/list` (or the SDK's schema) reflects exactly that. Test.
2. **TOTP accept/reject.** With an injected clock: a code for the current window
   is accepted; codes for window ±1 are accepted; a code for window ±2 and any
   wrong code are rejected as auth failures, and **no file is written** on
   rejection. The base32 secret decodes correctly (padding/normalization) — a
   generated reference code for a fixed timestamp matches. Tests for each case.
3. **`project_name` guard.** Names matching `^[A-Za-z0-9_-]+$` are accepted; each
   of empty, `foo.md` (dot), `a/b` (slash), `../x` (traversal), and a
   whitespace-containing name is rejected with a validation error and writes
   nothing. Tests per case, including explicit traversal attempts.
4. **Happy-path write.** A valid `token` + valid `project_name` + `content`
   writes `content` verbatim to `{folder}/{project_name}.md` inside the configured
   folder, and the success response contains the exact final filename. Test.
5. **No-clobber dedup.** When `{project_name}.md` already exists, the write goes to
   a fresh unique name via `O_CREAT | O_EXCL` (`{project_name}-2.md`, then `-3.md`,
   …); the pre-existing file's content is left untouched, and the response returns
   the exact de-duplicated filename. Test with a pre-planted file.
6. **Error responses don't leak the path.** Auth, validation, and write-error
   responses each clearly name which check failed, and the folder's absolute path
   never appears in any error text. Test asserts the configured folder path
   substring is absent from the error messages.
7. **Config via env.** The notes folder and bind host/port come from environment
   variables, all required with no default: an unset/non-existent notes folder,
   or an unset bind host/port, each fails clearly and non-silently at startup (no
   silent default). The server binds plain HTTP (no TLS in the process). Test the
   config loader's success plus the missing-folder and missing-port failures.
8. **Suite green + dependency hygiene.** `ruff`, `basedpyright`, and `pytest` all
   pass; the new package is typed and covered (each rejection branch + happy path
   + dedup). `mcp` is declared in `pyproject.toml` and is the only runtime
   dependency added; `basedpyright`'s `include` covers the new package.

## Known limitation (record honestly, do not silently harden)
Single-user by design: one secret, one folder. The secret is hardcoded in
committed source, so anyone with repo-read access — or anyone who can reach the
endpoint and knows the secret — can mint valid tokens; TOTP here is effectively a
shared static credential, not per-user auth. The real write-time safety boundary
is the `project_name` regex + no-clobber writer. This is acceptable for the stated
single-user VPS use; it is **not** safe to hand the secret to a second person
without adding per-user secrets and/or per-user folders. Leave that multi-user
path as an explicit follow-up, not an unstated assumption.

## Notes
- Keep the server package self-contained and importable so the tool handler, TOTP
  verifier, guard/writer, and config loader can each be unit-tested without
  standing up the HTTP transport. An end-to-end HTTP round-trip test is welcome
  but the unit-level tests above are the gate.
- Do not invent a second tool, a read/list path, or cert handling to "round it
  out" — Scope Out is deliberate. The public HTTPS/8443 surface is provided by the
  reverse proxy in front of this process.
