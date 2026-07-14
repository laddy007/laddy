"""Eval bundle model + outcome fold (orchestrator.oracle.evals)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.oracle.evals import (
    BUNDLE_EXPECTED,
    BUNDLE_SEED,
    BUNDLE_SPEC,
    EvalBundleError,
    bundle_dir,
    list_bundles,
    load_bundle,
)

SPEC_TEXT = """---
type: feature
roles: [developer, rw1]
---
# e1 - seeded eval

## Goal
f(x) must return abs(x).

## Acceptance
- f(-1) == 1
"""

SEED_PATCH = """diff --git a/impl.py b/impl.py
new file mode 100644
--- /dev/null
+++ b/impl.py
@@ -0,0 +1,2 @@
+def f(x):
+    return x  # seeded defect: wrong for x < 0
"""


def seed_registry(repo: Path) -> None:
    path = repo / TARGET_DIR_NAME / "oracle" / "classes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Escape classes\n\n- `edge-case` — unhandled boundary input\n",
        encoding="utf-8", newline="\n",
    )


def write_bundle(
    repo: Path,
    eval_id: str = "e1",
    *,
    spec_text: str = SPEC_TEXT,
    seed: str = SEED_PATCH,
    expected: dict[str, object] | None = None,
) -> Path:
    d = bundle_dir(repo, eval_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / BUNDLE_SPEC).write_text(spec_text, encoding="utf-8", newline="\n")
    (d / BUNDLE_SEED).write_text(seed, encoding="utf-8", newline="\n")
    payload = expected if expected is not None else {
        "class": "edge-case", "files": ["impl.py"], "note": "seeded from t1#1",
    }
    (d / BUNDLE_EXPECTED).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return d


def test_load_bundle_happy_path(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    write_bundle(tmp_path)
    bundle = load_bundle(tmp_path, "e1")
    assert bundle.eval_id == "e1"
    assert bundle.class_slug == "edge-case"
    assert bundle.files == ("impl.py",)
    assert "seeded defect" in bundle.seed_patch
    assert "rw1" in bundle.spec.roles


def test_load_bundle_missing_piece_is_actionable(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    d = write_bundle(tmp_path)
    (d / BUNDLE_SEED).unlink()
    with pytest.raises(EvalBundleError, match="seed.patch"):
        load_bundle(tmp_path, "e1")


def test_load_bundle_rejects_unregistered_class(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    write_bundle(tmp_path, expected={"class": "nope", "files": ["impl.py"]})
    with pytest.raises(EvalBundleError, match="nope"):
        load_bundle(tmp_path, "e1")


def test_load_bundle_rejects_empty_files(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    write_bundle(tmp_path, expected={"class": "edge-case", "files": []})
    with pytest.raises(EvalBundleError, match="files"):
        load_bundle(tmp_path, "e1")


def test_load_bundle_rejects_draft_and_report_only_specs(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    draft = SPEC_TEXT.replace("type: feature", "type: feature\nstatus: draft-proposal")
    write_bundle(tmp_path, spec_text=draft)
    with pytest.raises(EvalBundleError, match="draft"):
        load_bundle(tmp_path, "e1")
    audit = SPEC_TEXT.replace(
        "type: feature\nroles: [developer, rw1]", "type: audit"
    )
    write_bundle(tmp_path, "e2", spec_text=audit)
    with pytest.raises(EvalBundleError, match="report-only"):
        load_bundle(tmp_path, "e2")


def test_load_bundle_rejects_bad_eval_id(tmp_path: Path) -> None:
    with pytest.raises(EvalBundleError, match="eval id"):
        load_bundle(tmp_path, "Bad_Id")


def test_list_bundles(tmp_path: Path) -> None:
    seed_registry(tmp_path)
    write_bundle(tmp_path, "e2")
    write_bundle(tmp_path, "e1")
    assert list_bundles(tmp_path) == ["e1", "e2"]


from orchestrator.artifacts import SPEC, TaskArtifacts
from orchestrator.oracle.escapes import raise_oracle_escape
from orchestrator.oracle.evals import (
    CAUGHT,
    INCONCLUSIVE,
    MISSED,
    check_bundle,
    fold_outcome,
    scaffold_bundle,
)
from tests.fakes import git, init_repo, merge_agent_task


def _merged_task_with_escape(tmp_path: Path) -> Path:
    """Real repo: one merged agent task (spec committed) + one escape flag."""
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "registry")
    merge_agent_task(repo, "t1", {
        "impl.py": "def f(x):\n    return x\n",
        f"{TARGET_DIR_NAME}/tasks/t1/{SPEC}": "# t1\n\nAbs value.\n",
    })
    raise_oracle_escape(
        TaskArtifacts(repo, "t1"),
        class_slug="edge-case", grade="confirmed",
        summary="f(-1) returns -1", evidence="failing test",
    )
    return repo


def _verdict(
    verdict: str = "APPROVED", findings: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {"verdict": verdict, "risk_level": "low", "findings": findings or []}


def _blk(file: str = "impl.py") -> dict[str, object]:
    return {"severity": "blocker", "file": file, "line": 1,
            "summary": "seeded bug", "failure_scenario": "x", "category": "correctness"}


def _fold(entries, verdicts, terminal="CAP_REACHED", decision=None):
    return fold_outcome(
        eval_id="e1", class_slug="edge-case", expected_files=("impl.py",),
        entries=entries, verdicts=verdicts, terminal=terminal, decision=decision,
    )


def test_fold_caught_by_reviewer_blocker_on_seeded_file() -> None:
    out = _fold(
        [{"action": "rw1", "outcome": "changes_requested"}],
        {"rw1": _verdict("CHANGES_REQUESTED", [_blk("impl.py")])},
    )
    assert out.result == CAUGHT and out.caught_by == ("rw1",)


def test_fold_caught_by_red_test_gates() -> None:
    out = _fold(
        [{"action": "fast_tests", "outcome": "fail"},
         {"action": "authoritative", "outcome": "fail"}],
        {},
    )
    assert out.result == CAUGHT
    assert set(out.caught_by) == {"fast_tests", "authoritative"}


def test_fold_missed_when_all_judgment_gates_wave_it_through() -> None:
    out = _fold(
        [{"action": "rw1", "outcome": "approved"},
         {"action": "rw2", "outcome": "go"}],
        {"rw1": _verdict("APPROVED"), "rw2": _verdict("APPROVED")},
        terminal="MERGE_DECIDED:stop_before_merge",
        decision="stop_before_merge",
    )
    assert out.result == MISSED and out.caught_by == ()
    # decision-independence: stop_before_merge does NOT rescue a miss
    assert out.decision == "stop_before_merge"


def test_fold_missed_without_rw2_when_composition_had_none() -> None:
    out = _fold(
        [{"action": "rw1", "outcome": "approved"}],
        {"rw1": _verdict("APPROVED")},
        terminal="MERGE_DECIDED:stop_before_merge",
    )
    assert out.result == MISSED


def test_fold_inconclusive_on_off_target_blocker() -> None:
    out = _fold(
        [{"action": "rw1", "outcome": "changes_requested"}],
        {"rw1": _verdict("CHANGES_REQUESTED", [_blk("other.py")])},
    )
    assert out.result == INCONCLUSIVE
    assert any("other.py" in n for n in out.notes)


def test_fold_inconclusive_when_chain_never_completed() -> None:
    out = _fold([], {}, terminal="INTERNAL_ERROR")
    assert out.result == INCONCLUSIVE


def test_fold_inconclusive_when_rw1_approved_but_chain_interrupted() -> None:
    # rw1 approved and rw2 never ran - but the chain never reached a merge
    # decision (e.g. rw2 crashed), so this must NOT fold to MISSED: that
    # would falsely claim the gates waved the seed through.
    out = _fold(
        [{"action": "rw1", "outcome": "approved"}],
        {"rw1": _verdict("APPROVED")},
        terminal="INTERNAL_ERROR",
    )
    assert out.result == INCONCLUSIVE
    assert any("INTERNAL_ERROR" in n for n in out.notes)


def test_scaffold_bundle_prefills_from_escape(tmp_path: Path) -> None:
    repo = _merged_task_with_escape(tmp_path)
    d = scaffold_bundle(repo, task_id="t1", flag_id="t1#1", eval_id="e1")
    assert (d / BUNDLE_SPEC).read_text(encoding="utf-8").startswith("# t1")
    assert "impl.py" in (d / BUNDLE_SEED).read_text(encoding="utf-8")
    expected = json.loads((d / BUNDLE_EXPECTED).read_text(encoding="utf-8"))
    assert expected["class"] == "edge-case"
    assert expected["files"] == []  # deliberately empty: the author must anchor the seed
    # refuses to overwrite
    with pytest.raises(EvalBundleError, match="exists"):
        scaffold_bundle(repo, task_id="t1", flag_id="t1#1", eval_id="e1")


def test_scaffold_bundle_unknown_flag(tmp_path: Path) -> None:
    repo = _merged_task_with_escape(tmp_path)
    with pytest.raises(EvalBundleError, match="t1#9"):
        scaffold_bundle(repo, task_id="t1", flag_id="t1#9", eval_id="e1")


def test_scaffold_bundle_skip_clarify_task_falls_back_to_specs_dir(tmp_path: Path) -> None:
    # A --skip-clarify task has no tasks/<id>/spec.md at the merge sha; the
    # loop ran from specs/<id>.md - scaffold must use it, not raw-traceback.
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "registry")
    merge_agent_task(repo, "t-skip", {
        "impl.py": "def f(x):\n    return x\n",
        f"{TARGET_DIR_NAME}/specs/t-skip.md": "# t-skip\n\nAbs value.\n",
    })
    raise_oracle_escape(
        TaskArtifacts(repo, "t-skip"),
        class_slug="edge-case", grade="confirmed",
        summary="f(-1) returns -1", evidence="failing test",
    )
    d = scaffold_bundle(repo, task_id="t-skip", flag_id="t-skip#1", eval_id="e1")
    assert (d / BUNDLE_SPEC).read_text(encoding="utf-8").startswith("# t-skip")


def test_scaffold_bundle_no_spec_anywhere_is_actionable(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "registry")
    merge_agent_task(repo, "t-bare", {"impl.py": "x = 1\n"})
    raise_oracle_escape(
        TaskArtifacts(repo, "t-bare"),
        class_slug="edge-case", grade="confirmed",
        summary="s", evidence="e",
    )
    with pytest.raises(EvalBundleError, match="spec"):
        scaffold_bundle(repo, task_id="t-bare", flag_id="t-bare#1", eval_id="e1")


def test_check_bundle_flags_gaps_and_passes_a_good_bundle(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "registry")
    write_bundle(repo)  # SEED_PATCH adds impl.py; applies on main
    assert check_bundle(repo, "e1", tmp_path / "work") == []
    # expected file the patch never touches
    write_bundle(repo, "e2", expected={"class": "edge-case", "files": ["ghost.py"]})
    problems = check_bundle(repo, "e2", tmp_path / "work")
    assert any("ghost.py" in p for p in problems)
    # patch that cannot apply on main (context mismatch)
    bad = SEED_PATCH.replace("new file mode 100644\n", "").replace(
        "--- /dev/null", "--- a/impl.py"
    )
    write_bundle(repo, "e3", seed=bad)
    problems = check_bundle(repo, "e3", tmp_path / "work")
    assert any("apply" in p for p in problems)


def test_check_bundle_rejects_path_superset_match(tmp_path: Path) -> None:
    # expected "impl.py" must NOT be satisfied by a patch touching impl.pyx
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "registry")
    superset = SEED_PATCH.replace("impl.py", "impl.pyx")
    write_bundle(repo, seed=superset,
                 expected={"class": "edge-case", "files": ["impl.py"]})
    problems = check_bundle(repo, "e1", tmp_path / "work")
    assert any("impl.py" in p for p in problems)


def test_check_bundle_contains_missing_main(tmp_path: Path) -> None:
    # no 'main' branch: report a problem (never raise) and leave nothing
    # behind under work_root
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "registry")
    write_bundle(repo)
    git(repo, "branch", "-m", "main", "trunk")
    work = tmp_path / "work"
    problems = check_bundle(repo, "e1", work)
    assert problems  # non-empty, no exception
    assert not (work / "eval-check-e1").exists()
    assert not (work / "eval-check-e1.patch").exists()
