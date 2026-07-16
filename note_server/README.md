# note_server — TOTP-gated `save_note` MCP server

A small, self-contained MCP server exposing exactly one tool, `save_note`,
which writes a caller-supplied note to a `.md` file in one fixed, server-side
folder. Every call is authenticated with a TOTP code (RFC 6238) before anything
is written.

## Configuration (environment variables)

All three are **required, with no default** — startup fails clearly if any is
unset (or the folder is missing):

| Variable             | Meaning                                              |
|----------------------|------------------------------------------------------|
| `NOTE_SERVER_FOLDER` | Existing server-side directory the notes are written into. |
| `NOTE_SERVER_HOST`   | Plain-HTTP bind host (e.g. `127.0.0.1`).             |
| `NOTE_SERVER_PORT`   | Plain-HTTP bind port (integer, `1..65535`).          |

The TOTP shared secret is **hardcoded in `note_server/totp.py`** (single-user
design); it is *not* an environment variable.

## Running

```sh
NOTE_SERVER_FOLDER=/srv/notes \
NOTE_SERVER_HOST=127.0.0.1 \
NOTE_SERVER_PORT=8080 \
python -m note_server.server
```

The server binds **plain HTTP** and does not load certificates or terminate
TLS. The MCP SDK's Streamable-HTTP transport mounts at its default path,
**`/mcp`**, so the internal endpoint is `http://<host>:<port>/mcp`.

## TLS / the public 8443 endpoint

Put a **reverse proxy in front on port 8443** that terminates HTTPS and forwards
to the plain-HTTP bind above. The public address the agent calls is then:

```
https://<vps-host>:8443/mcp
```

TLS termination and certificate handling are the proxy's job, deliberately out
of scope for this process.

## Known limitation (single-user by design)

One secret, one folder. The secret is hardcoded in committed source, so anyone
with repo-read access — or anyone who can reach the endpoint and knows the
secret — can mint valid tokens; TOTP here is effectively a **shared static
credential**, not per-user auth. The real write-time safety boundary is the
`project_name` allowlist (`^[A-Za-z0-9_-]+$`) plus the no-clobber writer. This
is acceptable for the stated single-user VPS use; it is **not** safe to hand the
secret to a second person without adding per-user secrets and/or per-user
folders. That multi-user path is an explicit follow-up, not an unstated
assumption.
