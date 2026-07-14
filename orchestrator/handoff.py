"""Human-facing terminal artifacts + notifications (design doc S6, S8, S9):
human-summary.md, handback.md, ntfy push.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from orchestrator.artifacts import (
    HANDBACK,
    HUMAN_SUMMARY,
    RW1_VERDICT,
    RW2_VERDICT,
    TaskArtifacts,
)
from orchestrator.flags import open_flags


def _flags_section(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    """Rendered ``⚑ Flags`` section for the open flags, or [] when none.

    ``needs_director`` first, format ``- [kind] summary (id)`` with a
    ``note``/``detail`` follow-up line when present. Empty when there are
    no open flags, so the caller emits no bare heading.
    """
    flags = open_flags(entries)
    if not flags:
        return []
    lines = ["## ⚑ Flags", ""]
    for flag in flags:
        mark = " (needs-director)" if flag.needs_director else ""
        lines.append(f"- [{flag.kind}] {flag.summary} ({flag.id}){mark}")
        extra = flag.note or flag.detail
        if extra:
            # First line only, truncated - a multi-line detail must not inject
            # raw continuation lines (e.g. a spurious "## Rounds" heading) into
            # this one-screen section. Matches the round-trace detail rendering.
            first = str(extra).splitlines()[0][:200]
            lines.append(f"  {first}")
    lines.append("")
    return lines


def build_summary(
    task_id: str,
    terminal_state: str,
    entries: Sequence[Mapping[str, Any]],
    base_branch: str = "main",
    branch_remote_hint: str = "laddy",
) -> str:
    lines = [
        f"# Task {task_id} — {terminal_state}",
        "",
        f"Branch: `{task_id}`",
        f"Fetch: git fetch {branch_remote_hint} {task_id}  "
        f"(shows locally as {branch_remote_hint}/{task_id})",
        "",
        *_flags_section(entries),
        "## Rounds",
        "",
    ]
    for entry in entries:
        # Only round-trace entries (which carry an ``outcome``) belong here;
        # metadata-only events like flags render in the ⚑ Flags section above
        # and would otherwise show as garbled "-> ?" lines. Positive filter, so
        # any future metadata-only event kind is excluded by default.
        if "outcome" not in entry:
            continue
        action = entry.get("action", "?")
        outcome = entry.get("outcome", "?")
        detail = str(entry.get("detail", "")).strip()
        line = f"- {entry.get('ts', '')} `{action}` -> {outcome}"
        if detail:
            first = detail.splitlines()[0][:200]
            line += f" — {first}"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def write_human_summary(
    artifacts: TaskArtifacts,
    terminal_state: str,
    base_branch: str = "main",
    branch_remote_hint: str = "laddy",
) -> None:
    artifacts.write_text(
        HUMAN_SUMMARY,
        build_summary(
            artifacts.task_id, terminal_state, artifacts.read_log(), base_branch, branch_remote_hint
        ),
    )


# --- handback.md (design S6): one screen, Director acts from this -------------


def _verdict_line(artifacts: TaskArtifacts, name: str, label: str) -> str:
    verdict = artifacts.read_json(name)
    if not isinstance(verdict, dict):
        return f"- {label}: (none)"
    # Defensive: a corrupt verdict file (findings null, or a non-dict finding)
    # must not crash build_handback - this is the artifact that SUMMARIZES a
    # failed run, so it has to survive malformed input.
    findings = verdict.get("findings")
    findings = findings if isinstance(findings, list) else []
    blockers = [
        f.get("summary", "?")
        for f in findings
        if isinstance(f, dict) and f.get("severity") == "blocker"
    ]
    suffix = f" — blockers: {'; '.join(blockers[:3])}" if blockers else ""
    return f"- {label}: {verdict.get('verdict', '?')}{suffix}"


def build_handback(
    artifacts: TaskArtifacts,
    terminal_state: str,
    base_branch: str = "main",
    branch_remote_hint: str = "laddy",
) -> str:
    entries = artifacts.read_log()
    lines = [
        f"# Handback: {artifacts.task_id}",
        "",
        f"Final state: **{terminal_state}**",
        f"Branch with the diff: `{artifacts.task_id}`",
        f"Fetch: git fetch {branch_remote_hint} {artifacts.task_id}  "
        f"(shows locally as {branch_remote_hint}/{artifacts.task_id})",
        "",
        *_flags_section(entries),
        "## What was tried, per round",
        "",
    ]
    for entry in entries:
        # Only round-trace entries (carrying an ``outcome``) belong here; flags
        # and other metadata-only events render in the ⚑ Flags section above.
        if "outcome" not in entry:
            continue
        detail = str(entry.get("detail", "")).strip().splitlines()
        first = detail[0][:160] if detail else ""
        lines.append(
            f"- round {entry.get('round', '-')}: `{entry.get('action')}` -> "
            f"{entry.get('outcome')}{' — ' + first if first else ''}"
        )
    lines += ["", "## Latest verdicts", ""]
    lines.append(_verdict_line(artifacts, RW1_VERDICT, "rw1"))
    lines.append(_verdict_line(artifacts, RW2_VERDICT, "rw2"))
    last_failure = next(
        (
            e
            for e in reversed(entries)
            if e.get("outcome") == "fail" and e.get("action") in ("fast_tests", "authoritative")
        ),
        None,
    )
    if last_failure:
        lines += [
            "",
            f"## Last {last_failure['action']} failure (tail)",
            "",
            "```",
            str(last_failure.get("detail", ""))[-1500:],
            "```",
        ]
    lines.append("")
    return "\n".join(lines)


def write_handback(
    artifacts: TaskArtifacts,
    terminal_state: str,
    base_branch: str = "main",
    branch_remote_hint: str = "laddy",
) -> None:
    artifacts.write_text(
        HANDBACK, build_handback(artifacts, terminal_state, base_branch, branch_remote_hint)
    )


# --- ntfy notification (design S8) ---------------------------------------------

# Content policy: the topic is unauthenticated, so the message is BUILT ONLY
# from (task_id, state) - task name + terminal state + one neutral sentence.
# No diff content, no stack traces, no names, no secrets, no paths. Detail
# lives in human-summary.md / handback.md, which travel in git.
STATE_SENTENCES: dict[str, str] = {
    "PUSHED": "branch pushed, awaiting merge decision",
    "MERGE_DECIDED:auto_merge": "merged automatically",
    "MERGE_DECIDED:auto_merge_notify": "auto-merged, please review the change",
    "MERGE_DECIDED:stop_before_merge": "stopped before merge, your decision needed",
    "CAP_REACHED": "iteration cap reached, see handback",
    "ESCALATED_DEADLOCK": "escalation deadlock, see handback",
    "PATH_GUARD_VIOLATION": "report task touched source paths, stopped",
    "INVESTIGATOR_MALFORMED": "investigator output invalid, run stopped",
    "VERIFY_MALFORMED": "verify round output invalid, run stopped",
    "QUOTA_WAIT": "usage quota exhausted, loop paused until window reset",
    "QUOTA_TIMEOUT": "quota wait budget exhausted, see handback",
    "INTERNAL_ERROR": "run hit an internal error, see handback",
}

PostFn = Callable[[str, str], None]  # (url, message)


def _urllib_post(url: str, message: str) -> None:
    req = urllib.request.Request(
        url, data=message.encode("utf-8"), method="POST"
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


class NtfyNotifier:
    """One POST per terminal state; no-op without a configured topic."""

    def __init__(self, topic: str | None, post_fn: PostFn = _urllib_post) -> None:
        self._topic = topic
        self._post = post_fn

    def notify(self, task_id: str, terminal_state: str) -> None:
        if not self._topic:
            return
        sentence = STATE_SENTENCES.get(terminal_state, "run finished")
        try:
            self._post(f"https://ntfy.sh/{self._topic}", f"{task_id}: {sentence}")
        except OSError:
            # notification loss must never fail the run
            pass
