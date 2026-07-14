"""End-to-end CLI tests (orchestrator.oracle.cli) on real tmp repos."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import TaskArtifacts
from orchestrator.flags import derive_flags
from orchestrator.oracle.cli import main
from orchestrator.oracle.runlog import read_runs, watermark
from tests.fakes import git, init_repo, merge_agent_task

# The class registry and the phase-prompt template are both ENGINE
# resources now (orchestrator.oracle.classes.CLASSES_PATH /
# orchestrator.oracle.prompt.PROMPT_PATH read the real committed files
# under ENGINE_DIR directly) - `prepare` no longer reads either from
# --repo, so this fixture repo needs no local copy of them.


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def _merge_l2(repo: Path, task: str) -> str:
    return merge_agent_task(repo, task, {
        f"myapp/{task}.py": "def f() -> int:\n    return 1\n",
        f"{TARGET_DIR_NAME}/tasks/{task}/spec.md": f"# {task}\nAC: f returns 1\n",
        f"{TARGET_DIR_NAME}/tasks/{task}/iteration-log.jsonl": '{"action":"go"}\n',
    })


def test_status_without_watermark_is_due_exit_1(repo: Path, capsys) -> None:
    assert main(["status", "--repo", str(repo)]) == 1
    assert "no oracle run recorded" in capsys.readouterr().out


def test_record_run_sets_watermark_then_status_ok(repo: Path, capsys) -> None:
    start = git(repo, "rev-parse", "HEAD")
    _merge_l2(repo, "t1")
    assert main(["record-run", "--repo", str(repo), "--since", start,
                 "--to", git(repo, "rev-parse", "main")]) == 0
    [run] = read_runs(repo)
    assert run["reviewed"]["L2"] == ["t1"]
    assert watermark(repo) == git(repo, "rev-parse", "main")
    assert main(["status", "--repo", str(repo)]) == 0


def test_scope_lists_buckets(repo: Path, capsys) -> None:
    start = git(repo, "rev-parse", "HEAD")
    _merge_l2(repo, "t1")
    merge_agent_task(repo, "t-sens", {
        "myapp/models.py": "# sensitive\n",
        f"{TARGET_DIR_NAME}/tasks/t-sens/spec.md": "# s\n",
    })
    assert main(["scope", "--repo", str(repo), "--since", start]) == 0
    out = capsys.readouterr().out
    assert "L2" in out and "t1" in out
    assert "L3" in out and "t-sens" in out


def test_escape_raises_validated_flag_and_ledger_reports(repo: Path, capsys) -> None:
    _merge_l2(repo, "t1")
    assert main([
        "escape", "t1", "--repo", str(repo),
        "--class-slug", "regression", "--grade", "confirmed",
        "--summary", "f broke radius", "--evidence", "pytest fails: got 0",
        "--gate", "test",
    ]) == 0
    [flag] = [
        f for f in derive_flags(TaskArtifacts(repo, "t1").read_log())
        if f.kind == "oracle-escape"
    ]
    assert json.loads(flag.detail or "")["class"] == "regression"

    assert main(["ledger", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "regression" in out


def test_escape_rejects_unknown_task_and_creates_nothing(repo: Path) -> None:
    # A typo'd task id must be rejected: raise_flag's log_lock would
    # silently mkdir an orphan tasks/<typo>/ with a permanent open escape
    # the ledger counts but no record-run ever sees.
    with pytest.raises(SystemExit, match="unknown task"):
        main([
            "escape", "t1-typo", "--repo", str(repo),
            "--class-slug", "edge-case", "--grade", "confirmed",
            "--summary", "s", "--evidence", "e",
        ])
    assert not (repo / TARGET_DIR_NAME / "tasks" / "t1-typo").exists()


def test_escape_rejects_unregistered_slug(repo: Path) -> None:
    _merge_l2(repo, "t1")
    with pytest.raises(SystemExit):
        main([
            "escape", "t1", "--repo", str(repo),
            "--class-slug", "not-registered", "--grade", "confirmed",
            "--summary", "s", "--evidence", "e",
        ])


def test_record_run_collects_findings_from_reviewed_tasks(repo: Path) -> None:
    start = git(repo, "rev-parse", "HEAD")
    _merge_l2(repo, "t1")
    main([
        "escape", "t1", "--repo", str(repo),
        "--class-slug", "edge-case", "--grade", "plausible",
        "--summary", "s", "--evidence", "e",
    ])
    main(["record-run", "--repo", str(repo), "--since", start,
          "--to", git(repo, "rev-parse", "main")])
    [run] = read_runs(repo)
    assert run["findings"] == [{"task": "t1", "flag_id": "t1#1", "grade": "plausible"}]


def test_record_run_requires_explicit_to(repo: Path) -> None:
    # Default --to main would resolve the endpoint at RECORD time: merges
    # landing after the manual review session would be recorded as reviewed
    # with zero findings and the watermark would jump past them forever.
    # `scope` prints the exact sha to pin; record-run must demand it.
    with pytest.raises(SystemExit) as exc:
        main(["record-run", "--repo", str(repo), "--since", "whatever"])
    assert exc.value.code == 2


def test_record_run_does_not_reattribute_prior_escapes(repo: Path) -> None:
    # A follow-up merge of the SAME task id (normal rework) puts t1 back in
    # scope; the old escape belongs to the run that raised it and must not
    # be double-counted in the escape-rate series.
    start = git(repo, "rev-parse", "HEAD")
    _merge_l2(repo, "t1")
    main([
        "escape", "t1", "--repo", str(repo),
        "--class-slug", "edge-case", "--grade", "confirmed",
        "--summary", "s", "--evidence", "e",
    ])
    main(["record-run", "--repo", str(repo), "--since", start,
          "--to", git(repo, "rev-parse", "main")])
    merge_agent_task(repo, "t1", {
        "myapp/t1.py": "def f() -> int:\n    return 2\n",
    })
    main(["record-run", "--repo", str(repo),
          "--to", git(repo, "rev-parse", "main")])
    first, second = read_runs(repo)
    assert first["findings"] == [
        {"task": "t1", "flag_id": "t1#1", "grade": "confirmed"}
    ]
    assert second["findings"] == []


def test_record_run_pinned_to_ignores_merges_landing_after(repo: Path, capsys) -> None:
    # The manual AI-review session between `scope`/`prepare` and `record-run`
    # can span hours. If record-run recomputed the endpoint at commit time
    # (`rev-parse main`), a merge landing in that window would be silently
    # counted as "reviewed" though nobody reviewed it, and the watermark
    # would jump past it forever. Pinning `--to` at the sha `scope` printed
    # closes that race: the pinned run must not see t2 at all, and a later
    # `scope` (starting from the new watermark) must still find it.
    start = git(repo, "rev-parse", "HEAD")
    _merge_l2(repo, "t1")
    pinned = git(repo, "rev-parse", "main")  # what the Director copied from `scope`

    # A merge lands in the review window, after the endpoint was pinned.
    _merge_l2(repo, "t2")

    assert main([
        "record-run", "--repo", str(repo), "--since", start, "--to", pinned,
    ]) == 0
    [run] = read_runs(repo)
    assert run["to_sha"] == pinned
    reviewed_ids = {tid for ids in run["reviewed"].values() for tid in ids}
    skipped_ids = {tid for ids in run["skipped"].values() for tid in ids}
    assert "t2" not in reviewed_ids and "t2" not in skipped_ids

    # Watermark is now the pinned sha, not main's tip - t2 is not lost: a
    # follow-up scope (no --since, so it starts from the new watermark)
    # must list t2, never skip it forever.
    assert watermark(repo) == pinned
    assert main(["scope", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "t2" in out


def test_scope_with_unresolvable_watermark_exits_actionably(repo: Path) -> None:
    from orchestrator.oracle.runlog import append_run

    append_run(repo, from_sha="seed", to_sha="deadbeef" * 5,
               reviewed={}, skipped={}, findings=[],
               now=lambda: "2026-07-12T10:00:00Z")
    with pytest.raises(SystemExit, match="not resolvable"):
        main(["scope", "--repo", str(repo)])


def test_prepare_writes_prompts_and_clean_worktree(repo: Path, tmp_path: Path, capsys) -> None:
    start = git(repo, "rev-parse", "HEAD")
    _merge_l2(repo, "t1")
    work = tmp_path / "oracle-work"
    assert main([
        "prepare", "t1", "--repo", str(repo),
        "--since", start, "--work-root", str(work),
    ]) == 0
    phase1 = (work / "oracle-t1-phase1.md").read_text(encoding="utf-8")
    phase2 = (work / "oracle-t1-phase2.md").read_text(encoding="utf-8")
    assert "AC: f returns 1" in phase1          # spec is in
    assert "def f() -> int:" in phase1          # shipped diff is in
    assert '"action":"go"' not in phase1        # log is NOT in phase 1
    assert '"action":"go"' in phase2            # log IS in phase 2
    wt = work / "oracle-t1"
    files = sorted(
        p.relative_to(wt).as_posix()
        for p in (wt / TARGET_DIR_NAME / "tasks").rglob("*") if p.is_file()
    )
    assert files == [f"{TARGET_DIR_NAME}/tasks/t1/spec.md"]


def test_prepare_refuses_task_outside_reviewed_scope(repo: Path, tmp_path: Path) -> None:
    start = git(repo, "rev-parse", "HEAD")
    _merge_l2(repo, "t1")
    with pytest.raises(SystemExit, match="not in the reviewed scope"):
        main([
            "prepare", "nope", "--repo", str(repo),
            "--since", start, "--work-root", str(tmp_path / "w"),
        ])


def test_resolve_closes_flag(repo: Path) -> None:
    _merge_l2(repo, "t1")
    main([
        "escape", "t1", "--repo", str(repo),
        "--class-slug", "edge-case", "--grade", "plausible",
        "--summary", "s", "--evidence", "e",
    ])
    assert main([
        "resolve", "t1", "t1#1", "--repo", str(repo),
        "--resolution", "dismissed", "--note", "Director: not a real defect",
    ]) == 0
    [flag] = derive_flags(TaskArtifacts(repo, "t1").read_log())
    assert flag.status == "dismissed"


def test_repo_before_subcommand_is_a_loud_usage_error(repo: Path) -> None:
    # --repo lives on the subparsers only: given before the subcommand it
    # must fail loudly (argparse exit 2), never silently fall back to cwd.
    with pytest.raises(SystemExit) as excinfo:
        main(["--repo", str(repo), "status"])
    assert excinfo.value.code == 2


def test_cli_eval_new_scaffolds_and_hints(tmp_path, capsys) -> None:
    from tests.test_oracle_evals import (
        _merged_task_with_escape,
    )

    repo = _merged_task_with_escape(tmp_path)
    rc = main(["eval-new", "t1", "t1#1", "--eval-id", "e1", "--repo", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "seed.patch" in out and "eval-check" in out
    assert (repo / TARGET_DIR_NAME / "oracle" / "evals" / "e1" / "seed.patch").is_file()


def test_cli_eval_check_reports_problems(tmp_path, capsys) -> None:
    from tests.fakes import git, init_repo
    from tests.test_oracle_evals import seed_registry, write_bundle

    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    write_bundle(repo, expected={"class": "edge-case", "files": ["ghost.py"]})
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "bundle")
    rc = main(["eval-check", "e1", "--repo", str(repo),
               "--work-root", str(tmp_path / "w")])
    assert rc == 1
    assert "ghost.py" in capsys.readouterr().out


def test_cli_eval_run_exit_codes_and_wiring(tmp_path, capsys, monkeypatch) -> None:
    from orchestrator.oracle import cli as cli_mod
    from orchestrator.oracle.evals import EvalOutcome

    calls: dict[str, object] = {}

    def fake_run_eval(repo, eval_id, **kwargs):
        calls["eval_id"] = eval_id
        calls["fix_ref"] = kwargs["fix_ref"]
        return EvalOutcome(
            eval_id=eval_id, class_slug="edge-case", result="missed",
            caught_by=(), terminal="MERGE_DECIDED:stop_before_merge",
            decision="stop_before_merge", notes=(),
        )

    monkeypatch.setattr(cli_mod, "run_eval", fake_run_eval)
    monkeypatch.setattr(
        cli_mod, "default_tools", lambda config, repo, docker: object()
    )
    monkeypatch.setenv("AGENT_REPO_URL", "file:///tmp/hub.git")
    rc = main(["eval-run", "e1", "--fix-ref", "abc", "--repo", str(tmp_path)])
    assert rc == 1  # missed -> nonzero: the fix does NOT close the class
    assert calls == {"eval_id": "e1", "fix_ref": "abc"}
    assert "missed" in capsys.readouterr().out


def test_cli_eval_list(tmp_path, capsys) -> None:
    from tests.fakes import init_repo
    from tests.test_oracle_evals import seed_registry, write_bundle

    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    write_bundle(repo)
    rc = main(["eval-list", "--repo", str(repo)])
    assert rc == 0
    assert "e1" in capsys.readouterr().out
