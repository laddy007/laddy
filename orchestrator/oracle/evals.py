"""Seeded-eval bundles: the design's honesty rule made executable.

A prompt/role/.md fix for an escape class must never be validated by
"the text is there". It is validated by a seeded eval: a committed
bundle that replants a known defect into a sandboxed loop run
(orchestrator.oracle.evalrun) and mechanically checks whether the gates
now catch it. Bundle home: <agent-dir>/oracle/evals/<eval-id>/

  spec.md        the task spec the sandboxed loop sees (the cover story)
  seed.patch     the planted defect, committed as the developer output
  expected.json  {"class": <registered slug>, "files": [...], "note": ...}

The bundle rides the <agent-dir>/oracle/* sensitivity glob (the eval is
part of the measurement instrument - apex risk); the patch is inert data
in the repo and only ever APPLIES inside the sandbox.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import SPEC
from orchestrator.oracle import (
    ORACLE_DIR,
    add_detached_worktree,
    remove_worktree,
    run_git,
)
from orchestrator.oracle.classes import CLASSES_PATH, load_class_slugs
from orchestrator.oracle.escapes import iter_escapes
from orchestrator.oracle.inputs import merge_diff
from orchestrator.oracle.runlog import EVAL_RESULTS
from orchestrator.oracle.scope import merge_sha_for_task
from orchestrator.spec import SpecError, TaskSpec, parse_spec_text

EVALS_DIR = f"{ORACLE_DIR}/evals"
BUNDLE_SPEC = "spec.md"
BUNDLE_SEED = "seed.patch"
BUNDLE_EXPECTED = "expected.json"

# Outcome vocabulary is owned by the run-log event schema (one home).
CAUGHT, MISSED, INCONCLUSIVE = EVAL_RESULTS

# Eval ids become branch names (eval/<id>) and directory names.
_EVAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class EvalBundleError(ValueError):
    """Malformed or unrunnable eval bundle."""


def _require_eval_id(eval_id: str) -> None:
    if not _EVAL_ID_RE.match(eval_id):
        raise EvalBundleError(
            f"invalid eval id {eval_id!r}: lowercase slug (it becomes the "
            "eval/<id> branch and directory name)"
        )


@dataclass(frozen=True)
class EvalBundle:
    eval_id: str
    spec: TaskSpec
    spec_text: str
    seed_patch: str
    class_slug: str
    files: tuple[str, ...]
    note: str


def bundle_dir(repo_root: Path, eval_id: str) -> Path:
    return repo_root / EVALS_DIR / eval_id


def list_bundles(repo_root: Path) -> list[str]:
    root = repo_root / EVALS_DIR
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and (p / BUNDLE_EXPECTED).is_file()
    )


def load_bundle(repo_root: Path, eval_id: str) -> EvalBundle:
    """Load + validate one bundle; every failure names the fix."""
    _require_eval_id(eval_id)
    d = bundle_dir(repo_root, eval_id)
    for name in (BUNDLE_SPEC, BUNDLE_SEED, BUNDLE_EXPECTED):
        if not (d / name).is_file():
            raise EvalBundleError(f"{eval_id}: missing {name} in {d}")
    spec_text = (d / BUNDLE_SPEC).read_text(encoding="utf-8")
    try:
        spec = parse_spec_text(spec_text)
    except SpecError as exc:
        raise EvalBundleError(f"{eval_id}: invalid {BUNDLE_SPEC}: {exc}") from exc
    if spec.is_draft:
        raise EvalBundleError(
            f"{eval_id}: {BUNDLE_SPEC} is a draft - an eval spec is meant to "
            "run; drop the draft status"
        )
    if spec.report_only:
        raise EvalBundleError(
            f"{eval_id}: report-only task types have no code-review gate to "
            "measure; use a feature/bug spec"
        )
    if "rw1" not in spec.roles:
        raise EvalBundleError(
            f"{eval_id}: roles must include rw1 - a gate chain with no "
            "reviewer measures nothing"
        )
    seed_patch = (d / BUNDLE_SEED).read_text(encoding="utf-8")
    if not seed_patch.strip():
        raise EvalBundleError(f"{eval_id}: {BUNDLE_SEED} is empty")
    try:
        expected = json.loads((d / BUNDLE_EXPECTED).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalBundleError(
            f"{eval_id}: {BUNDLE_EXPECTED} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(expected, dict):
        raise EvalBundleError(f"{eval_id}: {BUNDLE_EXPECTED} must be an object")
    class_slug = expected.get("class")
    files = expected.get("files")
    if not isinstance(class_slug, str) or not class_slug:
        raise EvalBundleError(f"{eval_id}: expected.json needs a 'class' slug")
    slugs = load_class_slugs()
    if class_slug not in slugs:
        raise EvalBundleError(
            f"{eval_id}: unregistered class slug {class_slug!r}; register it "
            f"in {CLASSES_PATH} first (existing: {', '.join(slugs) or 'none'})"
        )
    if (
        not isinstance(files, list)
        or not files
        or not all(isinstance(f, str) and f for f in files)
    ):
        raise EvalBundleError(
            f"{eval_id}: expected.json 'files' must be a non-empty list of "
            "the paths carrying the seeded defect (the fold anchors reviewer "
            "blockers to them)"
        )
    note = expected.get("note")
    return EvalBundle(
        eval_id=eval_id,
        spec=spec,
        spec_text=spec_text,
        seed_patch=seed_patch,
        class_slug=class_slug,
        files=tuple(files),
        note=note if isinstance(note, str) else "",
    )


@dataclass(frozen=True)
class EvalOutcome:
    """Mechanical verdict of one seeded-eval run (pure fold, no I/O)."""

    eval_id: str
    class_slug: str
    result: str  # CAUGHT | MISSED | INCONCLUSIVE
    caught_by: tuple[str, ...]  # fast_tests | authoritative | rw1 | rw2 | senior
    terminal: str
    decision: str | None
    notes: tuple[str, ...]


def _blockers(verdict: Any) -> list[dict[str, Any]]:
    if not isinstance(verdict, dict):
        return []
    findings = verdict.get("findings")
    if not isinstance(findings, list):
        return []
    return [
        f for f in findings
        if isinstance(f, dict) and f.get("severity") == "blocker"
    ]


def fold_outcome(
    *,
    eval_id: str,
    class_slug: str,
    expected_files: Sequence[str],
    entries: Sequence[Mapping[str, Any]],
    verdicts: Mapping[str, Any],
    terminal: str,
    decision: str | None,
) -> EvalOutcome:
    """Did any gate catch the seeded defect? Pure, decision-independent.

    caught       - a test gate went red, or a reviewer blocker is anchored
                   in one of the seeded files.
    missed       - the chain COMPLETED (reached a merge decision) and every
                   judgment gate that ran waved the seed through (rw1
                   approved; rw2 go when it ran) and no test gate red.
    inconclusive - anything else: malformed/incomplete/interrupted chain
                   (e.g. a crash before reaching a merge decision), or
                   blockers only OUTSIDE the seeded surface (a real finding,
                   but not demonstrably the seeded one - widen
                   expected.files or trim the seed).

    The merge decision is recorded but never decides pass/fail: without the
    docker gate the decision can never be auto_merge, and a seed on a
    sensitive path always stops before merge - neither means "caught".
    """
    expected = set(expected_files)
    caught: list[str] = []
    notes: list[str] = []
    if any(e.get("action") == "fast_tests" and e.get("outcome") == "fail" for e in entries):
        caught.append("fast_tests")
    if any(e.get("action") == "authoritative" and e.get("outcome") == "fail" for e in entries):
        caught.append("authoritative")
    off_target = 0
    for gate in ("rw1", "rw2", "senior"):
        blockers = _blockers(verdicts.get(gate))
        if any(f.get("file") in expected for f in blockers):
            caught.append(gate)
        for finding in blockers:
            if finding.get("file") not in expected:
                off_target += 1
                notes.append(
                    f"{gate} blocker outside seeded surface: "
                    f"{finding.get('file')}: {finding.get('summary')}"
                )
    if caught:
        result = CAUGHT
    elif off_target:
        result = INCONCLUSIVE
    else:
        rw1_ok = any(
            e.get("action") == "rw1" and e.get("outcome") == "approved" for e in entries
        )
        rw2_ran = any(e.get("action") == "rw2" for e in entries)
        rw2_ok = any(
            e.get("action") == "rw2" and e.get("outcome") == "go" for e in entries
        )
        chain_completed = terminal.startswith("MERGE_DECIDED")
        if chain_completed and rw1_ok and (rw2_ok or not rw2_ran):
            result = MISSED
        else:
            result = INCONCLUSIVE
            notes.append(
                f"chain ended at {terminal} without a clean pass-through - "
                "inspect the sandbox artifacts (re-run with --keep)"
            )
    return EvalOutcome(
        eval_id=eval_id,
        class_slug=class_slug,
        result=result,
        caught_by=tuple(caught),
        terminal=terminal,
        decision=decision,
        notes=tuple(notes),
    )


def scaffold_bundle(
    repo_root: Path, *, task_id: str, flag_id: str, eval_id: str
) -> Path:
    """Prefill a bundle from a recorded escape (mechanical starting point).

    Cover story = the original task spec at the shipped merge; seed
    starting point = the SHIPPED merge diff (inputs.merge_diff reuse) -
    TRIM IT to the minimal defect, a whole-task seed measures noise, not
    the class; expected.json gets the flag's class and an EMPTY files
    list the author must fill (the fold anchors blockers to it).
    Crafting the final seed is judgment work; eval-check verifies the
    mechanics afterwards without burning agent tokens.
    """
    _require_eval_id(eval_id)
    record = next(
        (r for r in iter_escapes(repo_root)
         if r.task_id == task_id and r.flag_id == flag_id),
        None,
    )
    if record is None:
        raise EvalBundleError(
            f"no oracle-escape flag {flag_id!r} on task {task_id!r} "
            "(see: python -m orchestrator.oracle ledger)"
        )
    if record.class_slug is None:
        raise EvalBundleError(
            f"{flag_id} has no parseable class slug in its detail payload"
        )
    d = bundle_dir(repo_root, eval_id)
    if d.exists():
        raise EvalBundleError(f"bundle {eval_id!r} already exists at {d}")
    merge_sha = merge_sha_for_task(repo_root, task_id)
    if merge_sha is None:
        raise EvalBundleError(f"no merge commit for agent/{task_id} on main")
    # --skip-clarify tasks have no tasks/<id>/spec.md at the merge sha; the
    # loop ran from specs/<id>.md (same fallback as inputs.materialize_phase1).
    code, spec_text = run_git(
        repo_root, "show",
        f"{merge_sha}:{TARGET_DIR_NAME}/tasks/{task_id}/{SPEC}",
        check=False,
    )
    if code != 0:
        code, spec_text = run_git(
            repo_root, "show",
            f"{merge_sha}:{TARGET_DIR_NAME}/specs/{task_id}.md",
            check=False,
        )
    if code != 0:
        raise EvalBundleError(
            f"{task_id} has no committed spec at {merge_sha[:12]} (neither "
            f"tasks/{task_id}/{SPEC} nor specs/{task_id}.md)"
        )
    d.mkdir(parents=True)
    (d / BUNDLE_SPEC).write_text(spec_text + "\n", encoding="utf-8", newline="\n")
    (d / BUNDLE_SEED).write_text(
        merge_diff(repo_root, merge_sha) + "\n", encoding="utf-8", newline="\n"
    )
    (d / BUNDLE_EXPECTED).write_text(
        json.dumps(
            {"class": record.class_slug, "files": [], "note": record.summary},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8", newline="\n",
    )
    return d


def check_bundle(repo_root: Path, eval_id: str, work_root: Path) -> list[str]:
    """Mechanical validation, zero agent tokens. Empty list = ready to run.

    Load errors, expected files the patch never touches, and a test-apply
    of the seed on CURRENT main in a throwaway worktree.
    """
    try:
        bundle = load_bundle(repo_root, eval_id)
    except EvalBundleError as exc:
        return [str(exc)]
    problems: list[str] = []
    wt = work_root / f"eval-check-{eval_id}"
    work_root.mkdir(parents=True, exist_ok=True)
    patch = work_root / f"eval-check-{eval_id}.patch"
    try:
        patch.write_text(bundle.seed_patch, encoding="utf-8", newline="\n")
        # Ask git which paths the patch touches (structural - hand-parsing
        # '+++ b/...' headers mishandles renames and quoted paths).
        code, numstat = run_git(
            repo_root, "apply", "--numstat", str(patch), check=False
        )
        if code != 0:
            problems.append(f"{BUNDLE_SEED} is not a parseable patch")
            return problems
        touched = {
            line.split("\t", 2)[2]
            for line in numstat.splitlines()
            if line.count("\t") >= 2
        }
        for path in bundle.files:
            if path not in touched:
                problems.append(
                    f"expected file not touched by {BUNDLE_SEED}: {path}"
                )
        if add_detached_worktree(repo_root, "main", wt, check=False) != 0:
            problems.append(
                "could not materialize a 'main' worktree for the apply check"
            )
            return problems
        code, _ = run_git(wt, "apply", "--check", str(patch), check=False)
        if code != 0:
            problems.append(
                f"{BUNDLE_SEED} does not apply cleanly on main "
                "(git apply --check failed) - rebase the seed"
            )
    finally:
        patch.unlink(missing_ok=True)
        remove_worktree(repo_root, wt)
    return problems
