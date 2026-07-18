"""CLI for the oracle runbook: status -> scope -> prepare -> (fresh AI
session, manual) -> escape -> record-run -> ledger -> eval-new ->
eval-check -> eval-run -> eval-list; resolve when a fix + distillation
lands.  Usage: python -m orchestrator.oracle <subcommand>.

The RUN stays manual by design (the trigger is automated, judgment is the
Director's): ``prepare`` materializes the clean phase-1 input and writes
both phase prompts to files; a FRESH session outside the loop does the
reading; findings come back in via ``escape`` (validated against the class
registry); ``record-run`` appends the oracle-run event that advances the
watermark. File mutations here are appends to committed logs - commit them
directly to local main (append-only history addition; push stays with the
Director).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator import TARGET_DIR_NAME, default_work_root
from orchestrator.artifacts import (
    LOG,
    RW1_VERDICT,
    RW2_VERDICT,
    SENIOR_VERDICT,
    SPEC,
    TaskArtifacts,
)
from orchestrator.flags import FLAG_RESOLUTIONS, resolve_flag
from orchestrator.oracle import commit_exists, run_git
from orchestrator.oracle.classes import load_class_slugs
from orchestrator.oracle.escapes import (
    ATTRIBUTION_GATES,
    GRADES,
    derive_ledger,
    iter_escapes,
    raise_oracle_escape,
)
from orchestrator.oracle.evalrun import default_tools, run_eval
from orchestrator.oracle.evals import (
    CAUGHT,
    EvalBundleError,
    check_bundle,
    list_bundles,
    scaffold_bundle,
)
from orchestrator.oracle.inputs import materialize_phase1, merge_diff
from orchestrator.oracle.prompt import build_phase1_prompt, build_phase2_prompt
from orchestrator.oracle.runlog import (
    RUN_LOG_PATH,
    append_run,
    escape_rate_series,
    read_evals,
    read_runs,
    watermark,
)
from orchestrator.oracle.scope import (
    CALIBRATION,
    OracleScope,
    merged_tasks_in_range,
    select_scope,
)
from orchestrator.oracle.trigger import check as trigger_check

if TYPE_CHECKING:
    from collections.abc import Sequence


def _resolve_scope(
    repo: Path, since: str | None, to_ref: str = "main"
) -> tuple[str, OracleScope]:
    from_sha = since or watermark(repo)
    if from_sha is None:
        raise SystemExit(
            "no watermark yet - pass --since <sha> (e.g. the last externally "
            "reviewed commit) to define the first range"
        )
    if not commit_exists(repo, from_sha):
        raise SystemExit(
            f"range start {from_sha[:12]} is not resolvable in this clone - "
            "git history and the run log disagree; pass --since <reachable "
            "sha> to re-baseline"
        )
    return from_sha, select_scope(merged_tasks_in_range(repo, from_sha, to_ref))


def _work_root(repo: Path, args: argparse.Namespace, purpose: str) -> Path:
    root = args.work_root or default_work_root(repo, purpose)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cmd_status(repo: Path, args: argparse.Namespace) -> int:
    status = trigger_check(repo)
    verdict = "DUE" if status.due else "not due"
    print(f"[oracle] {verdict} (watermark: {status.watermark or 'none'})")
    for reason in status.reasons:
        print(f"  - {reason}")
    if not status.due:
        print(
            f"  - {status.merges_since} agent merges since watermark; "
            f"last run {status.days_since_last:.1f} days ago"
            if status.days_since_last is not None
            else f"  - {status.merges_since} agent merges since watermark"
        )
    return 1 if status.due else 0


def _cmd_scope(repo: Path, args: argparse.Namespace) -> int:
    from_sha, scope = _resolve_scope(repo, args.since, args.to)
    _, to_sha = run_git(repo, "rev-parse", args.to)
    print(
        f"[oracle] scope {from_sha[:12]}..{to_sha[:12]} ({CALIBRATION} mode)"
    )
    print(f"[oracle] pin this endpoint: record-run --to {to_sha}")
    for label, by_bucket in (
        ("reviewed", scope.reviewed_by_bucket()),
        ("skipped", scope.skipped_by_bucket()),
    ):
        for bucket in sorted(by_bucket):
            print(f"  {label} {bucket}: {', '.join(by_bucket[bucket])}")
    if not scope.reviewed and not scope.skipped:
        print("  (no agent merges in range)")
    return 0


def _read_verdicts_text(art: TaskArtifacts) -> str:
    parts: list[str] = []
    for name in (RW1_VERDICT, RW2_VERDICT, SENIOR_VERDICT):
        text = art.read_text(name)
        parts.append(f"### {name}\n\n{text if text is not None else '(missing)'}")
    return "\n\n".join(parts)


def _cmd_prepare(repo: Path, args: argparse.Namespace) -> int:
    task_id: str = args.task
    work_root = _work_root(repo, args, "oracle")
    _, scope = _resolve_scope(repo, args.since, args.to)
    by_id = {t.task_id: t for t in scope.reviewed}
    task = by_id.get(task_id)
    if task is None:
        skipped_ids = {t.task_id for t in scope.skipped}
        hint = (
            "it was skipped by the L1 sample (adjust scope.L1_SAMPLE_EVERY if "
            "L1 needs more scrutiny)" if task_id in skipped_ids
            else "not an agent merge in this range"
        )
        raise SystemExit(f"{task_id} is not in the reviewed scope: {hint}")
    slugs = load_class_slugs()
    wt = materialize_phase1(repo, task_id, task.merge_sha, work_root)
    spec_text = (
        wt / TARGET_DIR_NAME / "tasks" / task_id / SPEC
    ).read_text(encoding="utf-8")
    phase1 = build_phase1_prompt(
        task_id=task_id, spec_text=spec_text,
        diff_text=merge_diff(repo, task.merge_sha, task_id), worktree=wt,
        class_slugs=slugs,
    )
    art = TaskArtifacts(repo, task_id)
    phase2 = build_phase2_prompt(
        task_id=task_id,
        log_text=art.read_text(LOG) or "(missing)",
        verdicts_text=_read_verdicts_text(art),
        class_slugs=slugs,
    )
    p1 = work_root / f"oracle-{task_id}-phase1.md"
    p2 = work_root / f"oracle-{task_id}-phase2.md"
    p1.write_text(phase1, encoding="utf-8", newline="\n")
    p2.write_text(phase2, encoding="utf-8", newline="\n")
    print(f"[oracle] worktree: {wt}")
    print(f"[oracle] phase 1 prompt: {p1}")
    print(f"[oracle] phase 2 prompt: {p2}  (open ONLY after phase-1 findings are final)")
    return 0


def _cmd_escape(repo: Path, args: argparse.Namespace) -> int:
    art = TaskArtifacts(repo, args.task)
    # An escape attaches to a merged task's committed log. A typo'd id must
    # be rejected here: raise_flag's log_lock would silently mkdir an orphan
    # tasks/<typo>/ with a permanent open escape that the ledger counts but
    # no record-run ever includes (mirrors run.py's _phase_flag guard).
    if not art.log_path.is_file():
        raise SystemExit(
            f"unknown task {args.task!r}: no committed "
            f"{TARGET_DIR_NAME}/tasks/{args.task}/{LOG} on this checkout"
        )
    try:
        flag_id = raise_oracle_escape(
            art,
            class_slug=args.class_slug,
            grade=args.grade,
            summary=args.summary,
            evidence=args.evidence,
            gate=args.gate,
            attribution_note=args.attribution_note,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    log_rel = f"{TARGET_DIR_NAME}/tasks/{args.task}/{LOG}"
    print(f"[oracle] raised {flag_id} ({args.grade}, {args.class_slug})")
    # Commit the run-log provenance record ALONGSIDE the flag: the run log is
    # the authenticity anchor iter_escapes cross-checks, so a flag committed
    # without it would read as forged (uncounted) on a fresh checkout.
    print(f"[oracle] commit it: git add {log_rel} {RUN_LOG_PATH} && "
          f'git commit -m "oracle: escape {flag_id}"')
    return 0


def _cmd_resolve(repo: Path, args: argparse.Namespace) -> int:
    art = TaskArtifacts(repo, args.task)
    # This CLI is the Director channel - the one place allowed to close an
    # oracle-escape (the loop CLI's resolve path refuses the kind).
    if not resolve_flag(
        art, args.flag_id, resolution=args.resolution, note=args.note,
        allow_oracle_escape=True,
    ):
        raise SystemExit(f"no open flag {args.flag_id!r} on task {args.task!r}")
    print(f"[oracle] resolved {args.flag_id} ({args.resolution})")
    return 0


def _cmd_record_run(repo: Path, args: argparse.Namespace) -> int:
    # Resolve the endpoint FIRST and walk the range with the SHA: resolving
    # after the walk would let a merge landing in between be watermarked as
    # covered while appearing in neither reviewed nor skipped.
    _, to_sha = run_git(repo, "rev-parse", args.to)
    from_sha, scope = _resolve_scope(repo, args.since, to_sha)
    reviewed_ids = {t.task_id for t in scope.reviewed}
    # An escape belongs to the run that raised it: a task id re-entering a
    # later scope (rework merge) must not re-attribute its old escapes, or
    # escape_rate_series double-counts them.
    already_recorded = {
        (str(f.get("task")), str(f.get("flag_id")))
        for run in read_runs(repo)
        for f in run.get("findings", [])
    }
    findings = [
        {"task": r.task_id, "flag_id": r.flag_id, "grade": r.grade or "plausible"}
        for r in iter_escapes(repo)
        if r.task_id in reviewed_ids
        and (r.task_id, r.flag_id) not in already_recorded
    ]
    append_run(
        repo,
        from_sha=from_sha,
        to_sha=to_sha,
        reviewed=scope.reviewed_by_bucket(),
        skipped=scope.skipped_by_bucket(),
        findings=findings,
        mode=CALIBRATION,
    )
    print(f"[oracle] recorded run {from_sha[:12]}..{to_sha[:12]}: "
          f"{len(scope.reviewed)} reviewed, {len(scope.skipped)} skipped, "
          f"{len(findings)} finding(s)")
    print(f"[oracle] commit it: git add {RUN_LOG_PATH} {TARGET_DIR_NAME}/tasks && "
          f'git commit -m "oracle: run @ {to_sha[:12]}"')
    return 0


def _cmd_ledger(repo: Path, args: argparse.Namespace) -> int:
    records = iter_escapes(repo)
    entries = derive_ledger(records)
    if not entries:
        print("[oracle] ledger empty - no escapes recorded")
    for entry in entries:
        mark = "  << RECURRENT: confirmed upgrade target" if entry.recurrent else ""
        print(f"  {entry.class_slug}: {entry.total} total, {entry.open} open{mark}")
    for row in escape_rate_series(read_runs(repo), records):
        print(
            f"  run {row['ts']} {row['bucket']}: {row['escapes']} escape(s) / "
            f"{row['reviewed']} reviewed ({row['skipped']} skipped, "
            f"{row['pending']} pending)"
        )
    return 0


def _cmd_eval_new(repo: Path, args: argparse.Namespace) -> int:
    try:
        d = scaffold_bundle(
            repo, task_id=args.task, flag_id=args.flag_id, eval_id=args.eval_id
        )
    except EvalBundleError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"[oracle] scaffolded {d}")
    print("[oracle] now: TRIM seed.patch to the minimal defect, fill "
          "expected.json 'files', then: eval-check " + args.eval_id)
    return 0


def _cmd_eval_check(repo: Path, args: argparse.Namespace) -> int:
    problems = check_bundle(repo, args.eval_id, _work_root(repo, args, "eval"))
    if not problems:
        print(f"[oracle] {args.eval_id}: bundle OK (ready for eval-run)")
        return 0
    for problem in problems:
        print(f"[oracle] {args.eval_id}: {problem}")
    return 1


def _cmd_eval_run(repo: Path, args: argparse.Namespace) -> int:
    import os

    from orchestrator.config import OrchestratorConfig

    config = OrchestratorConfig.from_env(os.environ)
    tools = default_tools(config, repo, docker=args.docker)
    try:
        outcome = run_eval(
            repo,
            args.eval_id,
            work_root=_work_root(repo, args, "eval"),
            tools=tools,
            fast_commands=config.fast_commands,
            base=args.base,
            fix_ref=args.fix_ref,
            keep=args.keep,
            record=not args.no_record,
        )
    except EvalBundleError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"[oracle] eval {outcome.eval_id} ({outcome.class_slug}): "
          f"{outcome.result}")
    print(f"[oracle] caught by: {', '.join(outcome.caught_by) or '(nobody)'}; "
          f"terminal {outcome.terminal}; decision {outcome.decision}")
    for note in outcome.notes:
        print(f"[oracle]   note: {note}")
    if not args.no_record:
        print(f"[oracle] commit it: git add {RUN_LOG_PATH} && "
              f'git commit -m "oracle: seeded eval {outcome.eval_id}"')
    if outcome.result == CAUGHT:
        print("[oracle] fix validated - resolve the escape flag with "
              "--note referencing the fix commit and this eval")
        return 0
    print("[oracle] fix NOT validated - the class is still open")
    return 1


def _cmd_eval_list(repo: Path, args: argparse.Namespace) -> int:
    bundles = list_bundles(repo)
    if not bundles:
        print("[oracle] no eval bundles")
    for eval_id in bundles:
        print(f"  {eval_id}")
    for event in read_evals(repo):
        print(f"  run {event.get('ts')} {event.get('eval')}: "
              f"{event.get('result')} (caught by "
              f"{', '.join(event.get('caught_by', [])) or 'nobody'})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # ``--repo`` must work both before AND after the subcommand (every call
    # site in this module's tests passes it after, e.g. ``status --repo
    # x``); argparse subparsers only recognize options declared on the
    # subparser itself, so it is declared on a shared parent and inherited
    # by every subparser (not the top-level parser, because subparser
    # defaults silently clobber pre-subcommand values in the namespace).
    repo_parent = argparse.ArgumentParser(add_help=False)
    repo_parent.add_argument("--repo", default=".", type=Path)

    # Do NOT inherit repo_parent here: --repo before the subcommand must fail
    # loudly (argparse usage error), not silently fall back to cwd.
    parser = argparse.ArgumentParser(prog="orchestrator.oracle")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "status", parents=[repo_parent], help="is an oracle run due? (exit 1 = due)"
    ).set_defaults(handler=_cmd_status)
    p_scope = sub.add_parser(
        "scope", parents=[repo_parent], help="tasks the next run reviews/skips"
    )
    p_scope.set_defaults(handler=_cmd_scope)
    p_scope.add_argument("--since", default=None, help="range start sha (default: watermark)")
    p_scope.add_argument(
        "--to", default="main", help="range end ref/sha (default: main)"
    )
    p_prep = sub.add_parser(
        "prepare", parents=[repo_parent], help="materialize phase-1 input + write prompts"
    )
    p_prep.set_defaults(handler=_cmd_prepare)
    p_prep.add_argument("task")
    p_prep.add_argument("--since", default=None)
    p_prep.add_argument("--to", default="main", help="range end ref/sha (default: main)")
    p_prep.add_argument("--work-root", default=None, type=Path)
    p_esc = sub.add_parser(
        "escape", parents=[repo_parent], help="record one finding as an oracle-escape flag"
    )
    p_esc.set_defaults(handler=_cmd_escape)
    p_esc.add_argument("task")
    p_esc.add_argument("--class-slug", required=True)
    p_esc.add_argument("--grade", required=True, choices=GRADES)
    p_esc.add_argument("--summary", required=True)
    p_esc.add_argument("--evidence", required=True)
    p_esc.add_argument("--gate", default=None, choices=ATTRIBUTION_GATES)
    p_esc.add_argument("--attribution-note", default=None)
    p_res = sub.add_parser(
        "resolve", parents=[repo_parent], help="resolve/dismiss an oracle-escape flag"
    )
    p_res.set_defaults(handler=_cmd_resolve)
    p_res.add_argument("task")
    p_res.add_argument("flag_id")
    p_res.add_argument("--resolution", default="resolved", choices=FLAG_RESOLUTIONS)
    p_res.add_argument("--note", default=None, help="fix + distillation ref (commit/test)")
    p_rec = sub.add_parser(
        "record-run", parents=[repo_parent], help="append the oracle-run event (watermark)"
    )
    p_rec.set_defaults(handler=_cmd_record_run)
    p_rec.add_argument("--since", default=None)
    p_rec.add_argument(
        "--to", required=True,
        help="range end SHA - pin the endpoint `scope` printed, so the run "
        "records exactly what the manual AI-review session covered (a merge "
        "landing after the session must not be watermarked as reviewed)",
    )
    sub.add_parser(
        "ledger", parents=[repo_parent],
        help="per-class escape counts + escape-rate series",
    ).set_defaults(handler=_cmd_ledger)
    p_enew = sub.add_parser(
        "eval-new", parents=[repo_parent],
        help="scaffold a seeded-eval bundle from a recorded escape",
    )
    p_enew.set_defaults(handler=_cmd_eval_new)
    p_enew.add_argument("task")
    p_enew.add_argument("flag_id")
    p_enew.add_argument("--eval-id", required=True)
    p_echk = sub.add_parser(
        "eval-check", parents=[repo_parent],
        help="validate a bundle mechanically (no agent tokens); exit 1 = problems",
    )
    p_echk.set_defaults(handler=_cmd_eval_check)
    p_echk.add_argument("eval_id")
    p_echk.add_argument("--work-root", default=None, type=Path)
    p_erun = sub.add_parser(
        "eval-run", parents=[repo_parent],
        help="run the seeded eval in the sandbox; exit 0 = caught (fix validated)",
    )
    p_erun.set_defaults(handler=_cmd_eval_run)
    p_erun.add_argument("eval_id")
    p_erun.add_argument("--fix-ref", default=None,
                        help="commit sha of the fix being validated (honesty chain)")
    p_erun.add_argument("--base", default="main",
                        help="branch the eval bases on (default: main)")
    p_erun.add_argument("--docker", action="store_true",
                        help="include the authoritative docker gate (slow)")
    p_erun.add_argument("--keep", action="store_true",
                        help="keep the sandbox for inspection")
    p_erun.add_argument("--no-record", action="store_true",
                        help="do not append the seeded-eval event")
    p_erun.add_argument("--work-root", default=None, type=Path)
    sub.add_parser(
        "eval-list", parents=[repo_parent],
        help="bundles + past seeded-eval events",
    ).set_defaults(handler=_cmd_eval_list)

    args = parser.parse_args(argv)
    # Dispatch lives on the subparser (set_defaults) - one home per command,
    # no string re-matching. Work roots are resolved inside the commands
    # that use them (_work_root).
    return args.handler(args.repo.resolve(), args)
