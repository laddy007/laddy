"""Vendor-agnostic headless agent runner (design doc S11: agents.py).

Every LLM invocation goes through an ``AgentRunner``; unit tests inject
fakes so the loop never calls a real model. ``claude -p`` is the only
backend in Slice 1; Codex lands in Slice 2.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- Quota / rate-limit classification (spec: quota-resume-queue) -------------
#
# Conservative by design: "quota" may ONLY arise from an explicit pattern
# below; any other failure stays "error". The asymmetry is deliberate -- a
# missed quota just falls back to today's error handling (dev retries, then
# CAP_REACHED), but a FALSE quota parks the loop for up to
# QUOTA_MAX_WAIT_HOURS (30h). So the patterns are anchored to limit EVENTS,
# never a bare token: no naked `\b429\b` (matches "line 429" / "429 passed")
# and no naked "rate limit" (matches rate-limit *code* the dev may be writing).
QUOTA_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"usage limit",
        r"hit your limit",
        r"too many requests",  # also covers "429 Too Many Requests"
        r"quota exceeded",
        # "rate limit" only as a limit EVENT, not a passing code mention. The
        # event word must BIND to the limit token (directly follow it, via an
        # optional short linking verb) -- mere co-occurrence within N chars is
        # too loose and matches benign FAILED-agent output about rate-limit
        # CODE ("rate limiting middleware did not reset", "added rate limiting;
        # will retry"), which would falsely park the loop for up to 30h.
        r"rate.?limit(?:s|ed|ing)?\s+(?:is\s+|was\s+|has\s+been\s+|been\s+)?"
        r"(?:exceed(?:ed)?|reach(?:ed)?|hit|throttl(?:ed|ing)?|exhausted)\b",
        # a lone 429 only when it reads as an HTTP status.
        r"(?:http|status(?: code)?|error|code)\s+429\b",
    )
)
# `claude -p` reports subscription limits as "... usage limit reached|<epoch>".
_EPOCH_RE = re.compile(r"limit reached\|(\d{9,12})")
_RESETS_IN_RE = re.compile(r"resets? in (\d+)\s*(minutes?|mins?|hours?|hrs?)", re.IGNORECASE)
_RESETS_AT_ISO_RE = re.compile(
    r"resets? at (\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", re.IGNORECASE
)


@dataclass(frozen=True)
class QuotaSignal:
    """A recognized quota/rate-limit message; reset time when parseable."""

    reset_at: datetime | None


def detect_quota(text: str, now: datetime) -> QuotaSignal | None:
    """Classify agent output as a quota signal (None = not quota)."""
    if not any(p.search(text) for p in QUOTA_TEXT_PATTERNS):
        return None
    m = _EPOCH_RE.search(text)
    if m:
        # Parse defensively: a garbled/overflowing epoch must never raise out
        # of detect_quota and crash the detached loop -- fall through to the
        # other reset parses (ultimately QuotaSignal(None): still quota, just
        # unknown reset).
        try:
            return QuotaSignal(datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            pass
    # The ISO and relative parses are guarded like the epoch branch above: a
    # regex-matching but invalid date ("resets at 2026-99-99...") or an absurd
    # relative ("resets in 999999999 hours" -> OverflowError) must NOT raise out
    # of detect_quota and crash the detached loop; fall through to
    # QuotaSignal(None) (still quota, just unknown reset).
    m = _RESETS_AT_ISO_RE.search(text)
    if m:
        try:
            parsed = datetime.fromisoformat(m.group(1).replace(" ", "T"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return QuotaSignal(parsed)
        except ValueError:
            return QuotaSignal(None)
    m = _RESETS_IN_RE.search(text)
    if m:
        try:
            amount = int(m.group(1))
            minutes = amount * 60 if m.group(2).lower().startswith(("hour", "hr")) else amount
            return QuotaSignal(now + timedelta(minutes=minutes))
        except (ValueError, OverflowError):
            return QuotaSignal(None)
    return QuotaSignal(None)


DEFAULT_CLAUDE_CMD: tuple[str, ...] = (
    "claude",
    "-p",
    "--dangerously-skip-permissions",
    "--output-format",
    "json",
)

DEFAULT_CODEX_CMD: tuple[str, ...] = ("codex", "exec", "--full-auto")

# Senior reviewer escalation (design S13: senior = Claude Opus).
DEFAULT_SENIOR_CMD: tuple[str, ...] = DEFAULT_CLAUDE_CMD + ("--model", "claude-opus-4-8")

# rw2 reviewer. Historically the Codex cross-vendor guard; now Claude (Sonnet)
# because this deployment has no codex login. Override with RW2_CMD in env.vps.
# NOTE: this drops the cross-vendor property - rw1 and rw2 are both Claude now.
DEFAULT_RW2_CMD: tuple[str, ...] = DEFAULT_CLAUDE_CMD + ("--model", "sonnet")

# --- Least-privilege LOCAL review commands (trust-model S4/S10) ---------------
#
# The local panel (rw2 + security) reviews UNTRUSTED branch code on the
# Director's TRUSTED machine. A reviewer needs only to READ the diff and emit
# JSON; it must NOT hold write/exec grants, or a prompt-injection in the branch
# could turn the reviewer into a code-exec/exfiltration vector. So the local
# panel uses these read-only profiles, never the convergence commands above
# (which carry --dangerously-skip-permissions / --full-auto and are fine on the
# disposable VPS where there is no host to protect). Flags that grant write/exec
# are stripped by config even if an operator pastes them (see config.py).
DANGEROUS_AGENT_FLAGS: tuple[str, ...] = ("--dangerously-skip-permissions", "--full-auto")

# Read-only headless review: no skip-permissions; tools limited to reading.
DEFAULT_CLAUDE_REVIEW_CMD: tuple[str, ...] = (
    "claude", "-p", "--output-format", "json",
    "--allowedTools", "Read,Grep,Glob",
)
# Codex read-only sandbox (no workspace-write, no host mutation).
DEFAULT_CODEX_REVIEW_CMD: tuple[str, ...] = ("codex", "exec", "--sandbox", "read-only")
DEFAULT_SENIOR_REVIEW_CMD: tuple[str, ...] = DEFAULT_CLAUDE_REVIEW_CMD + (
    "--model", "claude-opus-4-8",
)

# (cmd, cwd, stdin) -> (returncode, stdout, stderr)
ExecFn = Callable[[Sequence[str], Path, str], tuple[int, str, str]]


@dataclass(frozen=True)
class AgentResult:
    """Typed outcome of one headless agent invocation."""

    text: str
    session_id: str | None
    exit_reason: str  # "ok" | "error" | "quota"
    returncode: int
    quota_reset_at: datetime | None = None


class AgentRunner(Protocol):
    name: str

    def run(self, prompt: str, cwd: Path, resume: str | None = None) -> AgentResult: ...


# A hung `claude`/`codex` (or a missing binary) must never wedge the DETACHED
# loop forever, and an exec-level failure must never crash out of it: both are
# mapped to a non-zero rc so the runner classifies them as exit_reason="error"
# (or "quota" if the message matches). Generous default so a legitimately long
# dev/review session is never killed; override with AGENT_EXEC_TIMEOUT_S.
_EXEC_TIMEOUT_S = int(os.environ.get("AGENT_EXEC_TIMEOUT_S", str(60 * 60)))


def _subprocess_exec(cmd: Sequence[str], cwd: Path, stdin: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=cwd,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=_EXEC_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        return 124, partial if isinstance(partial, str) else "", (
            f"agent timed out after {_EXEC_TIMEOUT_S}s"
        )
    except (FileNotFoundError, OSError) as exc:
        # e.g. the CLI binary is not on PATH: a failed run, not a crash.
        return 127, "", f"agent exec failed: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _error_kind(text: str, err: str, now: datetime) -> tuple[str, datetime | None]:
    """For a failed invocation: ("quota", reset_at) or ("error", None)."""
    sig = detect_quota(f"{text}\n{err}", now)
    if sig is not None:
        return "quota", sig.reset_at
    return "error", None


class ClaudeRunner:
    """Headless Claude Code: ``claude -p --output-format json`` (+ ``--resume``)."""

    name = "claude"

    def __init__(
        self,
        base_cmd: Sequence[str] = DEFAULT_CLAUDE_CMD,
        exec_fn: ExecFn = _subprocess_exec,
        now_fn: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._base_cmd = tuple(base_cmd)
        self._exec = exec_fn
        self._now = now_fn

    def run(self, prompt: str, cwd: Path, resume: str | None = None) -> AgentResult:
        cmd = list(self._base_cmd)
        if resume:
            cmd += ["--resume", resume]
        rc, out, err = self._exec(cmd, cwd, prompt)
        payload = None
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            pass
        if payload is None:
            # Non-JSON, or valid JSON that is not an object (null/str/array):
            # treat as plain text, never crash the detached loop.
            reason, reset_at = ("ok", None) if rc == 0 else _error_kind(out, err, self._now())
            return AgentResult(
                text=out if out else err,
                session_id=None,
                exit_reason=reason,
                returncode=rc,
                quota_reset_at=reset_at,
            )
        is_error = bool(payload.get("is_error")) or rc != 0
        session_id = payload.get("session_id")
        text = str(payload.get("result", ""))
        reason, reset_at = ("ok", None) if not is_error else _error_kind(text, err, self._now())
        return AgentResult(
            text=text,
            session_id=str(session_id) if session_id is not None else None,
            exit_reason=reason,
            returncode=rc,
            quota_reset_at=reset_at,
        )


class CodexRunner:
    """Cross-vendor guard backend: ``codex exec --full-auto`` (design S3, rw2).

    Codex has no session resume in this integration; the ``resume`` argument
    is accepted and ignored - artifact-first state makes that safe (S9).
    """

    name = "codex"

    def __init__(
        self,
        base_cmd: Sequence[str] = DEFAULT_CODEX_CMD,
        exec_fn: ExecFn = _subprocess_exec,
        now_fn: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._base_cmd = tuple(base_cmd)
        self._exec = exec_fn
        self._now = now_fn

    def run(self, prompt: str, cwd: Path, resume: str | None = None) -> AgentResult:
        rc, out, err = self._exec(list(self._base_cmd), cwd, prompt)
        reason, reset_at = ("ok", None) if rc == 0 else _error_kind(out, err, self._now())
        return AgentResult(
            text=out if out else err,
            session_id=None,
            exit_reason=reason,
            returncode=rc,
            quota_reset_at=reset_at,
        )
