"""Orchestrator CLI entrypoint (design doc S11: run.py).

Phases:
  clarify  interactive clarify gate over spec + fresh clone (SSH terminal)
  loop     detached autonomous loop (developer <-> tests <-> reviews -> push)
  all      clarify, then loop, in one process (useful locally)

kickoff.sh runs `clarify` in the foreground and detaches `loop`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from orchestrator import ENGINE_DIR, TARGET_DIR_NAME
from orchestrator.agents import (
    DEFAULT_CLAUDE_CMD,
    DEFAULT_CODEX_CMD,
    DEFAULT_RW2_CMD,
    DEFAULT_SENIOR_CMD,
    AgentRunner,
    ClaudeRunner,
    CodexRunner,
    set_model_flag,
)
from orchestrator.artifacts import ROLE_PLAN, TaskArtifacts
from orchestrator.clarify import has_clarify, run_clarify_gate
from orchestrator.config import OrchestratorConfig
from orchestrator.flags import (
    FLAG_ACTIONS,
    FLAG_RESOLUTIONS,
    LOOP_FLAG_KINDS,
    open_flags,
    raise_flag,
    resolve_flag,
)
from orchestrator.gitops import GitError, GitOps
from orchestrator.handoff import NtfyNotifier
from orchestrator.loop import Orchestrator, last_terminal_state
from orchestrator.policy import spec_is_high_risk
from orchestrator.queue import (
    QueueError,
    QueueLocked,
    TaskQueue,
    is_running,
    run_lock,
)
from orchestrator.quota import QuotaPolicy
from orchestrator.spec import SpecError, TaskSpec, parse_spec
from orchestrator.target_policy import load_target_policy
from orchestrator.terminals import clears_terminal, terminal_spec
from orchestrator.testgate import (
    DockerGate,
    ShellRunner,
    _subprocess_shell,
    _subprocess_shell_gate,
)


def _stdin_ask(question: str) -> str:
    print(f"\n[clarify] {question}")
    return input("[answer] > ")


def default_roles_dir() -> Path:
    """Roles are ENGINE resources (spec §3 step 0): the loop's personas come
    from the installed engine, never from the (untrusted) target worktree."""
    return ENGINE_DIR / "roles"


SPEC_AUTHOR_PROMPT = """\
You are co-authoring a task spec with the Director for the laddy agent
dev-loop over this target repo. The file {spec_rel} already exists with just a headline. Discuss
what the task should do, then fill in the rest of that file (Markdown;
optional front matter with type/roles above the headline). Do not implement
anything - only author the spec. Keep asking until the Director is
satisfied, then save and stop.
"""


def _default_author_spec(wt: Path, task_id: str, spec_rel: str) -> None:
    """Launch an INTERACTIVE Claude session (TUI, not headless -p) so the
    Director co-authors the spec in the terminal. Blocks until they exit."""
    prompt = SPEC_AUTHOR_PROMPT.format(spec_rel=spec_rel)
    subprocess.run(["claude", prompt], cwd=wt, check=False)


def _legacy_role_cmd(config: OrchestratorConfig, role: str) -> tuple[str, ...]:
    """The backward-compatible Claude command for a role when no ROLE_* vendor
    override is set. This is the ONLY per-role data left: rw2 and senior carry a
    different default model than the developer/rw1/clarify chain, driven by the
    legacy ``RW2_CMD``/``SENIOR_CMD``/``CLAUDE_CMD`` knobs. No runner selection
    branches on it -- that is uniform in `_resolve_runner`."""
    if role == "rw2":
        return config.rw2_cmd or DEFAULT_RW2_CMD
    if role == "senior":
        return config.senior_cmd or DEFAULT_SENIOR_CMD
    return config.claude_cmd or DEFAULT_CLAUDE_CMD


def _resolve_runner(config: OrchestratorConfig, role: str) -> AgentRunner:
    """One uniform role -> {vendor, model, thinking} -> runner resolver (spec
    fullrun-s0). With no ROLE_* env for a role the binding is absent, vendor
    defaults to claude, and the command is byte-for-byte the legacy per-role
    fallback -- so unconfigured deployments are unchanged."""
    binding = config.role_bindings.get(role)
    vendor = (binding.vendor if binding else None) or "claude"
    if vendor == "claude":
        cmd = _legacy_role_cmd(config, role)
        if binding and binding.model:
            cmd = set_model_flag(cmd, binding.model)
        # `claude -p` exposes no headless reasoning flag: thinking is a
        # documented no-op here (never an error), per the spec.
        return ClaudeRunner(cmd)
    cmd = config.codex_cmd or DEFAULT_CODEX_CMD
    if binding and binding.model:
        cmd = set_model_flag(cmd, binding.model)
    if binding and binding.thinking:
        # codex exec: reasoning effort via the `-c key=value` global override.
        cmd = (*cmd, "-c", f"model_reasoning_effort={binding.thinking}")
    return CodexRunner(cmd)


@dataclass
class Deps:
    """Injectable collaborators; tests replace these with fakes."""

    make_gitops: Callable[[OrchestratorConfig], GitOps] = lambda c: GitOps(
        c.repo_url, c.work_root, c.default_branch
    )
    # role -> runner: one resolver for every role (developer/rw1/clarify/rw2/
    # senior/...); the hardcoded per-role factories are gone (spec fullrun-s0).
    make_runner: Callable[[OrchestratorConfig, str], AgentRunner] = _resolve_runner
    ask: Callable[[str], str] = _stdin_ask
    shell: ShellRunner = field(default=_subprocess_shell)
    # Separate shell for the containerized authoritative gate: it orders stderr
    # before stdout so the pytest result survives into output_tail rather than
    # being buried under docker's stderr flood (see _subprocess_shell_gate).
    gate_shell: ShellRunner = field(default=_subprocess_shell_gate)
    author_spec: Callable[[Path, str, str], None] = field(default=_default_author_spec)


def _spec_rel(task_id: str) -> str:
    return f"{TARGET_DIR_NAME}/specs/{task_id}.md"


_DRAFT_REFUSAL = (
    "ERROR: spec is marked 'status: draft-proposal' - a merged fix proposal "
    "is a draft, not an approved spec. Promote it consciously (remove the "
    "draft status) before kickoff."
)


def _load_spec(wt: Path, task_id: str) -> tuple[TaskSpec | None, int]:
    """Return (spec, 0) or (None, exit_code). Enforces existence + draft gate."""
    spec_path = wt / _spec_rel(task_id)
    if not spec_path.is_file():
        print(f"ERROR: missing spec {spec_path}", file=sys.stderr)
        return None, 2
    try:
        spec = parse_spec(spec_path)
    except SpecError as exc:
        print(f"ERROR: invalid spec: {exc}", file=sys.stderr)
        return None, 2
    if spec.is_draft:
        print(_DRAFT_REFUSAL, file=sys.stderr)
        return None, 2
    return spec, 0


def _phase_new(config: OrchestratorConfig, task_id: str, deps: Deps) -> int:
    """Interactive spec authoring (trust-model doc S11): co-write the spec with
    the Director when none exists yet, then commit + push the branch so the
    normal clarify/loop can see it."""
    gitops = deps.make_gitops(config)
    wt = gitops.task_worktree(task_id)
    spec_path = wt / _spec_rel(task_id)
    if spec_path.is_file():
        print(
            f"ERROR: spec {spec_path.name} already exists on the branch - drop "
            "--new to run it, or pick a new task id.",
            file=sys.stderr,
        )
        return 2
    # seed the file with just a headline so the authoring session has a
    # concrete file to fill in (and the file always exists)
    seed = f"# {task_id}\n"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(seed, encoding="utf-8", newline="\n")
    deps.author_spec(wt, task_id, _spec_rel(task_id))
    if spec_path.read_text(encoding="utf-8") == seed:
        print(
            "ERROR: authoring session added nothing beyond the headline at "
            f"{_spec_rel(task_id)} - nothing to run.",
            file=sys.stderr,
        )
        return 2
    gitops.commit_all(wt, f"Author spec for {task_id} (--new)")
    gitops.push(wt, task_id)
    print(f"[new] spec authored and pushed on {task_id}")
    return 0


def _phase_clarify(
    config: OrchestratorConfig, task_id: str, deps: Deps, skip_clarify: bool
) -> int:
    gitops = deps.make_gitops(config)
    wt = gitops.task_worktree(task_id)
    _, rc = _load_spec(wt, task_id)
    if rc != 0:
        return rc
    artifacts = TaskArtifacts(wt, task_id)
    # Idempotent: never re-run the interactive gate on a task that already
    # cleared it (resume / re-kickoff), and honor --skip-clarify. Re-running
    # would re-interrogate the Director and append a duplicate
    # ## Clarifications block to the spec.
    if skip_clarify or has_clarify(artifacts.read_log()):
        print("[clarify] already done or skipped; proceeding")
        return 0
    spec_path = wt / _spec_rel(task_id)
    artifacts.copy_spec(spec_path)
    count = run_clarify_gate(
        deps.make_runner(config, "clarify"), wt, _spec_rel(task_id), deps.ask, artifacts
    )
    # keep the artifact copy in sync with the (possibly clarified) spec
    artifacts.copy_spec(spec_path)
    gitops.commit_all(wt, f"Clarify gate for {task_id} ({count} questions)")
    print(f"[clarify] done: {count} question(s)")
    return 0


# Actions that do NOT by themselves make a task in-progress: the pre-loop
# `clarify` gate and `flag`/`flag-resolved` events (raisable on a still-ready
# task). Everything else the loop writes (developer, explorer, investigator,
# verify, quota_*, path_guard, ...) is real progress. This is a DENYLIST, not
# an allowlist: an unrecognised action counts as progress, so a started task
# is never silently demoted to 'ready' and re-queued by enqueue --all.
_NON_PROGRESS_ACTIONS = frozenset({"clarify", "design", *FLAG_ACTIONS})


def _local_task_log(work_root: Path, task_id: str) -> list[dict[str, Any]]:
    """Read a task's iteration log from its node-local worktree, or [] when the
    worktree is absent on this node. Single reader of the ``wt/<task>`` log
    layout for read-only callers (status, flags reporter)."""
    wt = work_root / "wt" / task_id
    return TaskArtifacts(wt, task_id).read_log() if wt.is_dir() else []


def _derive_status(spec_path: Path, work_root: Path, queued: set[str]) -> str:
    """Derived, node-local task state (spec section 4). Order is load-bearing."""
    task_id = spec_path.stem
    try:
        spec = parse_spec(spec_path)
    except SpecError:
        return "unparseable"
    if spec.is_draft:
        return "draft"
    if spec.is_done:
        return "done"
    if is_running(work_root, task_id):
        return "running"
    if task_id in queued:
        return "queued"
    entries = _local_task_log(work_root, task_id)
    terminal = next((e for e in reversed(entries) if e.get("action") == "terminal"), None)
    if terminal is not None:
        outcome = str(terminal.get("outcome", "?"))
        if terminal_spec(outcome).kind == "success":
            return "pushed"
        return f"failed:{outcome}"
    if any(e.get("action") == "push" and e.get("outcome") == "ok" for e in entries):
        return "pushed"
    if any(e.get("action") not in _NON_PROGRESS_ACTIONS for e in entries):
        return "in-progress"
    return "ready"


def _phase_status(config: OrchestratorConfig, deps: Deps) -> int:
    gitops = deps.make_gitops(config)
    scan = gitops.refresh_base()
    queued = {item.task_id for item in TaskQueue(config.work_root).items()}
    for path in sorted((scan / TARGET_DIR_NAME / "specs").glob("*.md")):
        status = _derive_status(path, config.work_root, queued)
        print(f"{status:18s} {path.stem}")
    return 0


def _phase_flag(
    config: OrchestratorConfig, task_id: str, deps: Deps, args: argparse.Namespace
) -> int:
    """Raise or resolve a flag on <task>. Location matches --phase clarify:
    TaskArtifacts over the task's node-local worktree, no new store.

    Fast path when the worktree already exists on this node (the in-loop case):
    a pure local append, no git. Otherwise validate the task is real against
    the refreshed base BEFORE materializing a worktree - a typo'd id must be
    rejected, never silently branched into a junk worktree that strands the
    flag where no reporter can find it."""
    gitops = deps.make_gitops(config)
    wt = config.work_root / "wt" / task_id
    if not (wt / ".git").exists():
        base = gitops.refresh_base()
        if not (base / TARGET_DIR_NAME / "specs" / f"{task_id}.md").is_file():
            print(
                f"ERROR: unknown task {task_id!r}: no spec "
                f"{TARGET_DIR_NAME}/specs/{task_id}.md on {config.default_branch}",
                file=sys.stderr,
            )
            return 2
        wt = gitops.task_worktree(task_id)
    artifacts = TaskArtifacts(wt, task_id)
    if args.resolve is not None:
        try:
            resolved = resolve_flag(
                artifacts, args.resolve, resolution=args.resolution, note=args.note
            )
        except ValueError as exc:  # oracle-escape: Director channel only
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if not resolved:
            print(
                f"ERROR: no open flag {args.resolve!r} to resolve", file=sys.stderr
            )
            return 3
        print(f"[flag] resolved {args.resolve} ({args.resolution})")
        return 0
    flag_id = raise_flag(
        artifacts,
        args.kind,
        args.summary,
        detail=args.detail,
        round=args.round,
        needs_director=args.needs_director,
    )
    print(f"[flag] raised {flag_id}")
    return 0


def _phase_flags(config: OrchestratorConfig, task_ids: Sequence[str]) -> int:
    """Report OPEN flags only, grouped per task, needs-director first, with a
    short summary count. Sibling of --phase status; reads flags purely from
    node-local worktrees. No network: enumerating local ``wt/<task>`` dirs (not
    the remote spec list) means the reporter still works offline and can never
    fail its "always exit 0" contract on an unreachable origin."""
    if task_ids:
        tasks = list(task_ids)
    else:
        wt_root = config.work_root / "wt"
        tasks = (
            sorted(p.name for p in wt_root.iterdir() if p.is_dir())
            if wt_root.is_dir()
            else []
        )
    any_open = False
    for task_id in tasks:
        flags = open_flags(_local_task_log(config.work_root, task_id))
        if not flags:
            continue
        any_open = True
        needs = sum(1 for f in flags if f.needs_director)
        count = f"{len(flags)} open" + (f" ({needs} needs-director)" if needs else "")
        print(f"{task_id}: {count}")
        for flag in flags:
            mark = " [needs-director]" if flag.needs_director else ""
            print(f"  - [{flag.kind}] {flag.summary} ({flag.id}){mark}")
    if not any_open:
        print("no open flags")
    return 0


def _phase_resume(
    config: OrchestratorConfig, task_id: str, deps: Deps, reason: str | None
) -> int:
    """Director resume channel (director-resume): un-stick a finished task, hand
    the developer a written note, and continue the loop.

    Validation is all-or-nothing - NOTHING is appended until every check passes:
    a non-empty ``--reason``; the task exists and reached a terminal; that
    terminal is one ``director_resume`` clears (PATH_GUARD_VIOLATION and unknown
    states are refused - a poisoned/unknown tree is discarded, not resumed).
    Only then is one ``director_resume`` event appended (with the reason and a
    ``spec_sha`` receipt) and committed, and the loop started with clarify/design
    skipped (the task is already under way). This path never pushes to origin,
    never merges, never skips a reviewer - the resumed loop re-traverses every
    gate exactly as a fresh run.
    """
    text = (reason or "").strip()
    if not text:
        print("ERROR: --phase resume requires a non-empty --reason", file=sys.stderr)
        return 2
    gitops = deps.make_gitops(config)
    wt = gitops.task_worktree(task_id)
    spec, rc = _load_spec(wt, task_id)
    if rc != 0 or spec is None:
        return rc
    artifacts = TaskArtifacts(wt, task_id)
    state = last_terminal_state(artifacts.read_log())
    if state is None:
        print(
            f"ERROR: {task_id} has no recorded terminal to resume - it never "
            "started or is still running; kickoff/queue it normally instead.",
            file=sys.stderr,
        )
        return 2
    if not clears_terminal("director_resume", state):
        # Two distinct refusals: a RETRYABLE terminal (QUOTA_TIMEOUT /
        # INTERNAL_ERROR) is not poisoned - it already resumes on a plain
        # re-kickoff, so pointing at "discard the branch" would be wrong. A
        # sticky non-table state (PATH_GUARD_VIOLATION / unknown) IS the
        # discard-and-restart case.
        if not terminal_spec(state).sticky:
            print(
                f"ERROR: terminal {state} is transient and already resumable - "
                "just re-kickoff the task normally (no --phase resume needed).",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: terminal {state} is not resumable by --phase resume "
                "(PATH_GUARD_VIOLATION and unknown states are excluded by design: "
                "the tree is poisoned - discard the branch and restart).",
                file=sys.stderr,
            )
        return 2
    # Only now that the task is a validated, resumable terminal (not still
    # running, not poisoned) do we touch the tree: sync the persisted worktree to
    # the branch tip on origin. The Director corrects the ask by editing + pushing
    # the spec from a separate clone (USAGE.md §8), but task_worktree reuses this
    # worktree WITHOUT fetching - skip this and the developer reads the stale
    # pre-correction spec, spec_sha records the wrong blob, and the resumed run's
    # final push is rejected non-fast-forward. Done AFTER validation so a resume
    # of a still-running task (refused above) never hard-resets a live worktree.
    gitops.sync_worktree_to_origin(wt, task_id)
    spec_sha = gitops.blob_sha(wt, _spec_rel(task_id))
    artifacts.append_log(
        action="director_resume", outcome="ok", reason=text, spec_sha=spec_sha
    )
    gitops.commit_all(wt, f"Director resume for {task_id}")
    print(f"[resume] {task_id}: un-stuck {state}; starting loop")
    # The task already cleared clarify/design on its first run; skip them.
    return _phase_loop(config, task_id, deps, skip_clarify=True)


def _phase_loop(
    config: OrchestratorConfig,
    task_id: str,
    deps: Deps,
    skip_clarify: bool,
    code_ready: bool = False,
    base_task: str | None = None,
) -> int:
    try:
        with run_lock(config.work_root, task_id):
            return _phase_loop_locked(
                config, task_id, deps, skip_clarify,
                code_ready=code_ready, base_task=base_task,
            )
    except QueueLocked as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4


def _adopt_code_ready(gitops: GitOps, wt: Path, task_id: str) -> int:
    """--code-ready: adopt ALREADY-COMMITTED code on the task branch as round
    1's developer output, so the loop starts at the review chain (fast tests ->
    rw1 -> ...) instead of writing code (kickoff-on-finished-code).

    Validation is all-or-nothing - NOTHING is appended until every check
    passes: the task must be FRESH (no developer round, no recorded terminal -
    a task already under way resumes by re-kickoff or --phase resume, never by
    re-labeling its history), and the synced branch must actually differ from
    the base branch (an empty diff means there is nothing to review - refusing
    beats burning a review chain on it). Only then is one ``developer`` event
    appended (round 1, with a detail naming this adoption) and committed; the
    pure derivation then routes to fast_tests exactly as if a developer round
    had converged. No reviewer, gate, or policy step is skipped.
    """
    artifacts = TaskArtifacts(wt, task_id)
    entries = artifacts.read_log()
    if any(e.get("action") == "developer" for e in entries):
        print(
            f"ERROR: {task_id} already has a developer round - --code-ready "
            "adopts pre-existing code on a FRESH task only; re-kickoff or "
            "--phase resume continues an existing one.",
            file=sys.stderr,
        )
        return 2
    if last_terminal_state(entries) is not None:
        print(
            f"ERROR: {task_id} already reached a terminal - --code-ready "
            "adopts pre-existing code on a FRESH task only; use --phase "
            "resume to continue a finished task.",
            file=sys.stderr,
        )
        return 2
    # The finished code lives on the hub branch (committed there by the
    # Director); the reused worktree may predate that push, so sync first -
    # same reasoning as the resume channel's sync.
    gitops.sync_worktree_to_origin(wt, task_id)
    if not gitops.changed_files(wt, task_id):
        print(
            f"ERROR: {task_id} has no committed change against "
            f"{gitops.default_branch} - --code-ready needs the finished code "
            f"committed on the '{task_id}' branch (push it to the hub first).",
            file=sys.stderr,
        )
        return 2
    artifacts.append_log(
        action="developer",
        outcome="ok",
        round=1,
        detail="code-ready kickoff: pre-existing committed code adopted; "
        "developer phase skipped, review chain runs in full",
    )
    gitops.commit_all(wt, f"Adopt pre-existing code for {task_id} (--code-ready)")
    print(f"[code-ready] {task_id}: adopted committed code; starting at the review chain")
    return 0


def _build_orchestrator(
    config: OrchestratorConfig,
    deps: Deps,
    gitops: GitOps,
    wt: Path,
    spec: TaskSpec,
) -> Orchestrator:
    roles = spec.roles
    docker_gate = (
        DockerGate(
            frontend_gate=load_target_policy(wt).frontend_gate, shell=deps.gate_shell
        )
        if not spec.report_only
        else None
    )
    quota_policy = QuotaPolicy(
        reset_buffer=timedelta(seconds=config.quota_reset_buffer_s),
        backoff=tuple(timedelta(minutes=m) for m in config.quota_backoff_minutes),
        max_wait=timedelta(hours=config.quota_max_wait_hours),
    )
    return Orchestrator(
        gitops=gitops,
        dev_runner=deps.make_runner(config, "developer"),
        rw1_runner=deps.make_runner(config, "rw1"),
        rw2_runner=deps.make_runner(config, "rw2") if "rw2" in roles else None,
        senior_runner=deps.make_runner(config, "senior") if "rw2" in roles else None,
        docker_gate=docker_gate,
        composition=roles,
        policy_enabled=True,
        notifier=NtfyNotifier(config.ntfy_topic),
        fast_commands=config.fast_commands,
        shell=deps.shell,
        roles_dir=default_roles_dir(),
        max_loops=config.max_loops,
        quota_policy=quota_policy,
    )


def _phase_design(config: OrchestratorConfig, task_id: str, deps: Deps) -> int:
    """Foreground design gate (high-risk only): run the explorer, show the
    proposed approach, and record the Director's approve/reject. Non-high-risk
    tasks are a no-op. Idempotent: an already-approved task returns 0."""
    gitops = deps.make_gitops(config)
    wt = gitops.task_worktree(task_id)
    spec, rc = _load_spec(wt, task_id)
    if rc != 0 or spec is None:
        return rc
    spec_text = (wt / _spec_rel(task_id)).read_text(encoding="utf-8")
    if not spec_is_high_risk(load_target_policy(wt), spec_text, spec.risk):
        print("[design] task is not high-risk; no design gate needed")
        return 0
    artifacts = TaskArtifacts(wt, task_id)
    if any(e.get("action") == "design" and e.get("outcome") == "approved"
           for e in artifacts.read_log()):
        print("[design] approach already approved")
        return 0
    approach = _build_orchestrator(config, deps, gitops, wt, spec).run_explorer(task_id)
    print("\n===== PROPOSED APPROACH (high-risk task) =====\n")
    print(approach)
    print("\n==============================================\n")
    answer = deps.ask("Approve this approach? Type 'approve', or a rejection reason:")
    if answer.strip().lower() == "approve":
        artifacts.append_log(action="design", outcome="approved")
        gitops.commit_all(wt, f"Design approved for {task_id}")
        print(f"[design] approved for {task_id}")
        return 0
    artifacts.append_log(action="design", outcome="rejected", reason=answer.strip()[:2000])
    gitops.commit_all(wt, f"Design rejected for {task_id}")
    print(f"[design] rejected for {task_id}: {answer.strip()}", file=sys.stderr)
    return 5


def _phase_loop_locked(
    config: OrchestratorConfig,
    task_id: str,
    deps: Deps,
    skip_clarify: bool,
    code_ready: bool = False,
    base_task: str | None = None,
) -> int:
    gitops = deps.make_gitops(config)
    wt = gitops.task_worktree(task_id, base_task=base_task)
    artifacts = TaskArtifacts(wt, task_id)
    if not skip_clarify and not has_clarify(artifacts.read_log()):
        print(
            "ERROR: clarify gate has not run for this task "
            "(run --phase clarify first, or pass --skip-clarify)",
            file=sys.stderr,
        )
        return 2
    # Draft gate is re-checked here too: --phase loop --skip-clarify bypasses
    # _phase_clarify entirely, so without this a draft-proposal spec would run.
    spec, rc = _load_spec(wt, task_id)
    if rc != 0 or spec is None:
        return rc
    spec_text = (wt / _spec_rel(task_id)).read_text(encoding="utf-8")
    if spec_is_high_risk(load_target_policy(wt), spec_text, spec.risk) and not any(
        e.get("action") == "design" and e.get("outcome") == "approved"
        for e in artifacts.read_log()
    ):
        print(
            "ERROR: high-risk task requires design approval "
            "(run --phase design first)",
            file=sys.stderr,
        )
        return 2
    # After the clarify/draft/design validations (a code-ready task honors
    # every gate the written-by-the-loop path does), before the loop starts.
    if code_ready:
        rc = _adopt_code_ready(gitops, wt, task_id)
        if rc != 0:
            return rc
    artifacts.write_json(ROLE_PLAN, spec.role_plan(task_id))
    orchestrator = _build_orchestrator(config, deps, gitops, wt, spec)
    terminal = orchestrator.run(task_id)
    print(f"[loop] terminal state: {terminal}")
    if terminal == "PUSHED" or terminal.startswith("MERGE_DECIDED:"):
        return 0
    return 1


def _phase_enqueue(
    config: OrchestratorConfig,
    task_ids: Sequence[str],
    deps: Deps,
    skip_clarify: bool,
    chain: bool = False,
) -> int:
    """Queue READY tasks: spec exists, not draft, clarify already answered
    (a queued task may start at 3am with nobody to ask) - or the Director
    explicitly opts out with --skip-clarify. Validation is all-or-nothing:
    one invalid task means nothing is queued (no surprising partial state).
    A status: done spec IS allowed here - a deliberate re-run.

    ``chain`` links the listed tasks in the given order: each task's worktree
    starts from its predecessor's pushed branch, and the queue runner stops
    the chain's remainder when a link fails (the items stay queued for the
    Director). The chain is exactly the argument order - one enqueue call,
    one chain."""
    gitops = deps.make_gitops(config)
    queue = TaskQueue(config.work_root)
    already = {item.task_id for item in queue.items()}
    # A task id repeated WITHIN this call is a validation error, not a partial
    # enqueue: without this guard the first repeat would write its file and the
    # second would raise an uncaught QueueError from queue.enqueue() below,
    # leaving one item queued - a direct all-or-nothing (AC4) violation.
    if len(set(task_ids)) != len(task_ids):
        dupes = sorted({t for t in task_ids if task_ids.count(t) > 1})
        print(
            f"ERROR: duplicate task id(s) in one call: {', '.join(dupes)}; "
            "nothing queued",
            file=sys.stderr,
        )
        return 2
    # Chain mode must NOT create task worktrees during validation: a fresh
    # worktree would be cut from the default branch NOW and would later shadow
    # the chain base (an existing worktree wins over it in task_worktree).
    # Chain specs are validated from the base clone; the per-task log
    # (clarify/design markers) is read from an EXISTING worktree only.
    scan = gitops.refresh_base() if chain else None
    for task_id in task_ids:
        if scan is not None:
            root = scan
            existing_wt = config.work_root / "wt" / task_id
            entries = (
                TaskArtifacts(existing_wt, task_id).read_log()
                if (existing_wt / ".git").exists()
                else []
            )
        else:
            root = gitops.task_worktree(task_id)
            entries = TaskArtifacts(root, task_id).read_log()
        spec, rc = _load_spec(root, task_id)
        if rc != 0:
            print(f"ERROR: {task_id}: invalid spec, nothing queued", file=sys.stderr)
            return rc
        if task_id in already:
            print(f"ERROR: {task_id}: already queued, nothing queued", file=sys.stderr)
            return 2
        if not skip_clarify and not has_clarify(entries):
            print(
                f"ERROR: {task_id}: clarify gate has not run - run "
                "`--phase clarify` first, or enqueue with --skip-clarify "
                "(explicit Director choice); nothing queued",
                file=sys.stderr,
            )
            return 2
        # High-risk tasks need an approved design BEFORE they can be queued -
        # the same gate _phase_loop_locked enforces at run time and
        # _phase_enqueue_all pre-checks, so an explicit-id enqueue cannot slip a
        # high-risk task past the design-approval gate into the queue.
        if spec is not None:
            spec_text = (root / _spec_rel(task_id)).read_text(encoding="utf-8")
            if spec_is_high_risk(load_target_policy(root), spec_text, spec.risk) and not any(
                e.get("action") == "design" and e.get("outcome") == "approved"
                for e in entries
            ):
                print(
                    f"ERROR: {task_id}: high-risk task requires design approval "
                    "(run `--phase design` first); nothing queued",
                    file=sys.stderr,
                )
                return 2
    for pos, task_id in enumerate(task_ids):
        base_task = task_ids[pos - 1] if chain and pos > 0 else None
        try:
            item = queue.enqueue(
                task_id, skip_clarify=skip_clarify, base_task=base_task
            )
        except QueueError as exc:
            # only reachable via a race with another queue writer between the
            # validation loop above and here - all-or-nothing still holds for
            # everything already queued by THIS call up to the failing item.
            print(f"ERROR: {task_id}: {exc}", file=sys.stderr)
            return 2
        print(f"[enqueue] queued as {item.path.name}")
    return 0


def _parse_selection(raw: str, count: int) -> list[int]:
    """Parse a picker answer like '1 3-5' into 1-based indices (pure)."""
    picked: list[int] = []
    for token in raw.replace(",", " ").split():
        lo, dash, hi = token.partition("-")
        try:
            start = int(lo)
            end = int(hi) if dash else start
        except ValueError as exc:
            raise ValueError(f"not a number or range: {token!r}") from exc
        if start > end:
            raise ValueError(f"descending range: {token!r}")
        if start < 1 or end > count:
            raise ValueError(f"out of range 1-{count}: {token!r}")
        picked.extend(range(start, end + 1))
    return picked


def _enqueue_candidates(
    scan: Path, work_root: Path, queue: TaskQueue, *, include_in_progress: bool
) -> list[tuple[str, str]]:
    """(task_id, status) pairs queueable now: 'ready' always; 'in-progress'
    only for --pick (resuming is a deliberate choice).

    Unparseable specs are skipped with a warning - discovery must not die
    on one broken file."""
    queued = {item.task_id for item in queue.items()}
    wanted = ("ready", "in-progress") if include_in_progress else ("ready",)
    out: list[tuple[str, str]] = []
    for path in sorted((scan / TARGET_DIR_NAME / "specs").glob("*.md")):
        status = _derive_status(path, work_root, queued)
        if status == "unparseable":
            print(f"[enqueue] WARNING: skipping unparseable {path.name}")
            continue
        if status in wanted:
            out.append((path.stem, status))
    return out


def _phase_enqueue_all(config: OrchestratorConfig, deps: Deps, skip_clarify: bool) -> int:
    """--all: queue every ready candidate; skip unclarified with a warning
    (unattended collection must not fail on one unready spec)."""
    gitops = deps.make_gitops(config)
    queue = TaskQueue(config.work_root)
    scan = gitops.refresh_base()  # base clone hard-synced to origin/<default>
    ready: list[str] = []
    for task_id, _status in _enqueue_candidates(
        scan, config.work_root, queue, include_in_progress=False
    ):
        wt = gitops.task_worktree(task_id)
        artifacts = TaskArtifacts(wt, task_id)
        if not skip_clarify and not has_clarify(artifacts.read_log()):
            print(f"[enqueue] WARNING: {task_id} skipped - clarify gate has not run")
            continue
        spec, _rc = _load_spec(wt, task_id)
        if spec is not None:
            spec_text = (wt / _spec_rel(task_id)).read_text(encoding="utf-8")
            if spec_is_high_risk(load_target_policy(wt), spec_text, spec.risk) and not any(
                e.get("action") == "design" and e.get("outcome") == "approved"
                for e in artifacts.read_log()
            ):
                print(
                    f"[enqueue] WARNING: {task_id} skipped - high-risk, "
                    "design not approved"
                )
                continue
        ready.append(task_id)
    for task_id in ready:
        try:
            item = queue.enqueue(task_id, skip_clarify=skip_clarify)
        except QueueError as exc:
            # enqueue now serializes under queue.lock(); a concurrent queue
            # runner holding it (QueueLocked) or a racing writer (dup) stops the
            # rest of the batch cleanly rather than tracebacking.
            print(f"ERROR: {task_id}: {exc}", file=sys.stderr)
            return 2
        print(f"[enqueue] queued as {item.path.name}")
    if not ready:
        print("[enqueue] nothing ready to queue")
    return 0


def _phase_enqueue_pick(config: OrchestratorConfig, deps: Deps, skip_clarify: bool) -> int:
    """--pick: numbered candidate list, Director selects via deps.ask."""
    gitops = deps.make_gitops(config)
    queue = TaskQueue(config.work_root)
    scan = gitops.refresh_base()
    candidates = _enqueue_candidates(scan, config.work_root, queue, include_in_progress=True)
    if not candidates:
        print("[enqueue] no candidates")
        return 0
    for pos, (task_id, status) in enumerate(candidates, 1):
        mark = " [in-progress]" if status == "in-progress" else ""
        print(f"{pos:2d}. {task_id}{mark}")
    raw = deps.ask("Which tasks to queue? (e.g. '1 3-5'; empty = none)")
    try:
        picked = _parse_selection(raw, count=len(candidates))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not picked:
        print("[enqueue] nothing selected")
        return 0
    selected = [candidates[i - 1][0] for i in picked]
    return _phase_enqueue(config, selected, deps, skip_clarify=skip_clarify)


def _phase_queue(config: OrchestratorConfig, deps: Deps) -> int:
    """Single-flight queue runner: process items FIFO until empty. An item
    is removed after ANY terminal state (a failed task is not re-queued -
    the Director gets the ntfy + handback exactly as with a direct run).

    Chain semantics (enqueue --chain): an item carrying ``base_task`` runs
    only after that predecessor SUCCEEDED in this pass (pushed its branch).
    When a link fails, is deferred, or is missing, every transitive descendant
    is left in the queue with a warning - chained tasks build on their
    predecessor's code, so running them without it would review the wrong
    tree. Independent items keep running; the Director decides about the
    stopped chain (fix + re-run --phase queue, or dequeue by hand)."""
    queue = TaskQueue(config.work_root)
    # rc==4 means the task's per-task run lock is held by a concurrent direct
    # run: it was SKIPPED, not run to a terminal, so it must stay in the queue
    # (removing it here silently loses the task if that other run later fails).
    # Track deferred ids so we skip past them to the next runnable item instead
    # of busy-looping on items[0], and stop when only deferred items remain.
    deferred: set[str] = set()
    # task_id -> why its chain descendants must not run in this pass
    blocked: dict[str, str] = {}
    try:
        with queue.lock():
            while True:
                items = [it for it in queue.items() if it.task_id not in deferred]
                if not items:
                    if deferred:
                        print(
                            f"[queue] {len(deferred)} task(s) left in queue "
                            "(run lock held / chain stopped); re-run "
                            "--phase queue later"
                        )
                    else:
                        print("[queue] empty, done")
                    return 0
                item = items[0]
                if item.base_task is not None and item.base_task in blocked:
                    print(
                        f"[queue] WARNING: {item.task_id} LEFT in queue - "
                        f"chain stopped: predecessor {item.base_task} "
                        f"{blocked[item.base_task]}"
                    )
                    blocked[item.task_id] = (
                        f"blocked (upstream {item.base_task} "
                        f"{blocked[item.base_task]})"
                    )
                    deferred.add(item.task_id)
                    continue
                # Chained item with a REUSED worktree: task_worktree applies
                # the chain base only to a fresh one, so a worktree created
                # earlier (clarify, a plain kickoff) may not contain the
                # predecessor's tip - running it would review the wrong tree.
                # Fail closed: leave it queued and stop the chain.
                existing_wt = config.work_root / "wt" / item.task_id
                if item.base_task is not None and (existing_wt / ".git").exists():
                    gitops = deps.make_gitops(config)
                    gitops.ensure_base()  # fetch: origin/<base> must be current
                    if not gitops.chain_base_satisfied(existing_wt, item.base_task):
                        print(
                            f"[queue] WARNING: {item.task_id} LEFT in queue - "
                            f"existing worktree {existing_wt} does not contain "
                            f"chain base '{item.base_task}'; remove the "
                            "worktree for a fresh start from the chain base"
                        )
                        blocked[item.task_id] = "failed (stale worktree without chain base)"
                        deferred.add(item.task_id)
                        continue
                print(
                    f"[queue] running {item.task_id}"
                    + (f" (chained on {item.base_task})" if item.base_task else "")
                )
                try:
                    rc = _phase_loop(
                        config, item.task_id, deps,
                        skip_clarify=True, base_task=item.base_task,
                    )
                except GitError as exc:
                    # e.g. the chain base branch is missing on origin: the
                    # item never ran, so it STAYS queued and stops its chain.
                    print(f"ERROR: {item.task_id}: {exc}", file=sys.stderr)
                    blocked[item.task_id] = "failed (chain base unavailable)"
                    deferred.add(item.task_id)
                    continue
                print(f"[queue] {item.task_id} finished rc={rc}")
                if rc == 4:
                    print(
                        f"[queue] WARNING: {item.task_id} skipped and LEFT in "
                        "queue - another loop holds its run lock"
                    )
                    blocked[item.task_id] = "deferred (run lock held elsewhere)"
                    deferred.add(item.task_id)
                    continue
                if rc != 0:
                    # The task reached a terminal (ntfy + handback fired), so
                    # per queue semantics it leaves the queue - but its branch
                    # is not a trustworthy chain base, so descendants stop.
                    blocked[item.task_id] = f"failed (rc={rc})"
                queue.remove(item)
    except QueueLocked as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


def _phase_queue_list(config: OrchestratorConfig) -> int:
    items = TaskQueue(config.work_root).items()
    if not items:
        print("[queue] empty")
        return 0
    for pos, item in enumerate(items, 1):
        flags = ", skip-clarify" if item.skip_clarify else ""
        chain = f", chained on {item.base_task}" if item.base_task else ""
        print(f"{pos:2d}. {item.task_id}  (enqueued {item.enqueued_at}{flags}{chain})")
    return 0


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    deps: Deps | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator.run")
    parser.add_argument("task_ids", nargs="*", metavar="task_id")
    parser.add_argument(
        "--phase",
        choices=(
            "new", "clarify", "design", "loop", "all", "resume", "enqueue", "queue",
            "queue-list", "status", "flag", "flags",
        ),
        default="all",
    )
    parser.add_argument("--new", action="store_true", help="author the spec interactively first")
    parser.add_argument("--skip-clarify", action="store_true")
    parser.add_argument(
        "--code-ready",
        dest="code_ready",
        action="store_true",
        help="loop phase: the finished code is already committed on the task "
        "branch - adopt it as round 1's developer output and start at the "
        "review chain (fast tests -> rw1 -> ...)",
    )
    parser.add_argument(
        "--chain",
        action="store_true",
        help="enqueue phase: link the listed tasks in the given order - each "
        "task's worktree starts from its predecessor's pushed branch, and a "
        "failed link stops the chain's remainder (items stay queued)",
    )
    parser.add_argument(
        "--reason", help="resume phase: the Director's note (why the task is resumed)"
    )
    # --phase flag: raise (--kind/--summary/...) or resolve (--resolve) a flag
    parser.add_argument(
        "--kind", choices=LOOP_FLAG_KINDS,
        help="flag phase: flag kind (raise; oracle-escape is Director-only)",
    )
    parser.add_argument("--summary", help="flag phase: one-line flag summary (raise)")
    parser.add_argument("--detail", help="flag phase: optional longer detail (raise)")
    parser.add_argument("--round", type=int, help="flag phase: optional originating round (raise)")
    parser.add_argument(
        "--needs-director", dest="needs_director", action="store_true",
        help="flag phase: flag awaits a Director decision (raise)",
    )
    parser.add_argument("--resolve", metavar="ID", help="flag phase: resolve flag <ID>")
    parser.add_argument(
        "--resolution", choices=FLAG_RESOLUTIONS, default="resolved",
        help="flag phase: how to resolve (default: resolved)",
    )
    parser.add_argument("--note", help="flag phase: optional resolution note")
    parser.add_argument(
        "--pick", action="store_true",
        help="enqueue only: interactive selection from ready candidates",
    )
    parser.add_argument(
        "--all", dest="enqueue_all", action="store_true",
        help="enqueue only: queue every ready candidate (skips unclarified)",
    )
    args = parser.parse_args(argv)

    single_task_phases = ("new", "clarify", "design", "loop", "all", "resume", "flag")
    if args.phase in single_task_phases and len(args.task_ids) != 1:
        parser.error(f"--phase {args.phase} requires exactly one task id")
    if (args.pick or args.enqueue_all) and args.phase != "enqueue":
        parser.error("--pick/--all are only valid with --phase enqueue")
    if args.phase == "flag":
        raise_opts_given = (
            args.kind is not None
            or args.summary is not None
            or args.detail is not None
            or args.needs_director
            or args.round is not None
        )
        if args.resolve is not None:
            # resolve and raise are mutually exclusive modes
            if raise_opts_given:
                parser.error(
                    "--resolve (resolve mode) cannot be combined with raise "
                    "options (--kind/--summary/--detail/--round/--needs-director)"
                )
        else:
            # --note is the resolution note (resolve mode only). In raise mode
            # it has no home and was silently dropped - reject it instead.
            if args.note is not None:
                parser.error(
                    "--note is a resolution note, valid only with --resolve; "
                    "use --detail for context when raising a flag"
                )
            if args.kind is None or not (args.summary or "").strip():
                parser.error(
                    "--phase flag requires either --resolve <id>, or --kind and "
                    "a non-empty --summary to raise a flag"
                )
    if args.phase == "enqueue":
        modes = sum((bool(args.task_ids), args.pick, args.enqueue_all))
        if modes != 1:
            parser.error(
                "--phase enqueue takes EXACTLY ONE of: explicit task ids, --pick, --all"
            )
        if args.chain and not args.task_ids:
            parser.error("--chain requires an explicit ordered task list")
        if args.chain and len(args.task_ids) < 2:
            parser.error("--chain needs at least two tasks to link")
    elif args.chain:
        parser.error("--chain is only valid with --phase enqueue")
    if args.phase in ("queue", "queue-list", "status") and args.task_ids:
        parser.error(f"--phase {args.phase} takes no task id")
    # kickoff.sh forwards the same args to clarify/design/loop, so those
    # phases ACCEPT (and ignore) --code-ready; refuse it only where it could
    # mislead - the queue family and resume have their own start semantics.
    if args.code_ready and args.phase in (
        "new", "resume", "enqueue", "queue", "queue-list", "status", "flag", "flags"
    ):
        parser.error(f"--code-ready is not valid with --phase {args.phase}")

    config = OrchestratorConfig.from_env(env if env is not None else os.environ)
    deps = deps or Deps()

    if args.phase == "enqueue":
        if args.enqueue_all:
            return _phase_enqueue_all(config, deps, skip_clarify=args.skip_clarify)
        if args.pick:
            return _phase_enqueue_pick(config, deps, skip_clarify=args.skip_clarify)
        return _phase_enqueue(
            config, args.task_ids, deps,
            skip_clarify=args.skip_clarify, chain=args.chain,
        )
    if args.phase == "queue":
        return _phase_queue(config, deps)
    if args.phase == "queue-list":
        return _phase_queue_list(config)
    if args.phase == "status":
        return _phase_status(config, deps)
    if args.phase == "flags":
        return _phase_flags(config, args.task_ids)
    if args.phase == "flag":
        return _phase_flag(config, args.task_ids[0], deps, args)

    task_id = args.task_ids[0]

    if args.phase == "new" or (args.new and args.phase == "all"):
        rc = _phase_new(config, task_id, deps)
        if rc != 0 or args.phase == "new":
            return rc
    if args.phase in ("clarify", "all"):
        rc = _phase_clarify(config, task_id, deps, skip_clarify=args.skip_clarify)
        if rc != 0 or args.phase == "clarify":
            return rc
    if args.phase == "design":
        return _phase_design(config, task_id, deps)
    if args.phase == "resume":
        return _phase_resume(config, task_id, deps, args.reason)
    return _phase_loop(
        config, task_id, deps,
        skip_clarify=args.skip_clarify, code_ready=args.code_ready,
    )


if __name__ == "__main__":
    raise SystemExit(main())
