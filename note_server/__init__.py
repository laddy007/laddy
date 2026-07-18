"""TOTP-gated MCP server exposing a single ``save_note`` tool.

The package is split so every security-relevant piece (TOTP verifier,
``project_name`` guard + no-clobber writer, env-var config loader, and the
tool handler) is importable and unit-testable without standing up the HTTP
transport. ``server.build_server`` wires them into a FastMCP app.
"""
