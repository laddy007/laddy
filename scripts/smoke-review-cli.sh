#!/usr/bin/env bash
# Smoke-check the least-privilege LOCAL review commands (trust-model S4/S10)
# actually run to completion against the real, installed CLIs -- not just
# the unit-tested security invariant (no write/exec grant), which is a
# separate property from "the process runs and returns a usable verdict".
# See TODO.md "agent-loop-hardening follow-ups (2)".
#
# Exercises orchestrator.agents.ClaudeRunner / CodexRunner with the exact
# DEFAULT_*_REVIEW_CMD tuples the orchestrator itself uses -- not a
# hand-rolled reimplementation -- against a trivial, real, read-only prompt
# in this repo. Skips (not fails) a vendor whose CLI isn't on PATH, so it is
# safe to run on a machine that only has one of the two installed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$(cd "$ENGINE_DIR/.." && pwd)"

ENGINE_DIR="$ENGINE_DIR" python3 - "$@" <<'PYEOF'
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["ENGINE_DIR"])

from orchestrator.agents import (  # noqa: E402
    ClaudeRunner,
    CodexRunner,
    DEFAULT_CLAUDE_REVIEW_CMD,
    DEFAULT_CODEX_REVIEW_CMD,
)

PROMPT = (
    "Read README.md in the current directory (read-only -- you have no "
    "write or exec tools). Reply with exactly one line: the text of its "
    "first heading, nothing else."
)

import shutil

results = {}

if shutil.which("claude"):
    print("=== claude (DEFAULT_CLAUDE_REVIEW_CMD) ===")
    print(" ".join(DEFAULT_CLAUDE_REVIEW_CMD))
    r = ClaudeRunner(DEFAULT_CLAUDE_REVIEW_CMD).run(PROMPT, Path.cwd())
    print(f"exit_reason={r.exit_reason} returncode={r.returncode} session_id={r.session_id}")
    print(f"text={r.text!r}")
    results["claude"] = r.exit_reason == "ok" and r.returncode == 0 and bool(r.text.strip())
else:
    print("=== claude: SKIPPED (not on PATH) ===")
    results["claude"] = None

print()

if shutil.which("codex"):
    print("=== codex (DEFAULT_CODEX_REVIEW_CMD) ===")
    print(" ".join(DEFAULT_CODEX_REVIEW_CMD))
    r = CodexRunner(DEFAULT_CODEX_REVIEW_CMD).run(PROMPT, Path.cwd())
    print(f"exit_reason={r.exit_reason} returncode={r.returncode}")
    print(f"text={r.text!r}")
    results["codex"] = r.exit_reason == "ok" and r.returncode == 0 and bool(r.text.strip())
else:
    print("=== codex: SKIPPED (not on PATH) ===")
    results["codex"] = None

print()
print("=== summary ===")
failed = False
for vendor, ok in results.items():
    if ok is None:
        print(f"{vendor}: SKIPPED")
    elif ok:
        print(f"{vendor}: PASS (ran to completion, non-empty verdict text)")
    else:
        print(f"{vendor}: FAIL")
        failed = True

if failed:
    sys.exit(1)
if all(v is None for v in results.values()):
    print("Nothing to check -- neither CLI is on PATH.")
    sys.exit(2)
PYEOF
