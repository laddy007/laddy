"""Sandboxed seeded-eval harness (orchestrator.oracle.evalrun)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.oracle.evalrun import (
    EvalGitOps,
    NeverRunner,
    cleanup_sandbox,
    make_sandbox,
)
from tests.fakes import git, init_repo


def test_never_runner_raises() -> None:
    with pytest.raises(RuntimeError, match="developer"):
        NeverRunner().run("prompt", Path("."))


def test_eval_gitops_uses_eval_namespace(tmp_path: Path) -> None:
    ops = EvalGitOps(repo_url="unused", work_root=tmp_path)
    assert ops._branch("e1") == "eval/e1"


def test_make_sandbox_origin_is_the_local_hub(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    sandbox = make_sandbox(repo, tmp_path / "work")
    assert sandbox.hub == tmp_path / "work" / "eval-hub.git"
    assert (sandbox.hub / "HEAD").is_file()  # bare clone
    wt = sandbox.gitops.task_worktree("e1")
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "eval/e1"
    # the sandbox's only remote is the hub - it never learns a real one
    assert Path(git(wt, "remote", "get-url", "origin")).resolve() == sandbox.hub.resolve()
    # a push from inside the sandbox lands in the hub, nowhere else
    sandbox.gitops.push(wt, "e1")
    assert "eval/e1" in git(sandbox.hub, "for-each-ref", "--format=%(refname:short)")
    assert "eval/e1" not in git(repo, "for-each-ref", "--format=%(refname:short)")


def test_make_sandbox_is_fresh_each_run(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    make_sandbox(repo, tmp_path / "work")
    sandbox = make_sandbox(repo, tmp_path / "work")  # no "already exists" crash
    assert (sandbox.hub / "HEAD").is_file()


def test_cleanup_sandbox_removes_everything(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    sandbox = make_sandbox(repo, tmp_path / "work")
    sandbox.gitops.task_worktree("e1")
    cleanup_sandbox(tmp_path / "work")
    assert not sandbox.hub.exists()
    assert not (tmp_path / "work" / "base").exists()
    assert not (tmp_path / "work" / "wt").exists()


from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import TaskArtifacts
from orchestrator.loop import derive_resume_point
from orchestrator.oracle.evalrun import plant_seed
from orchestrator.oracle.evals import EvalBundleError, load_bundle
from tests.test_oracle_evals import seed_registry, write_bundle


def _repo_with_bundle(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    write_bundle(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "bundle + registry")
    return repo


def test_plant_seed_commits_seed_as_developer_output(tmp_path: Path) -> None:
    repo = _repo_with_bundle(tmp_path)
    bundle = load_bundle(repo, "e1")
    sandbox = make_sandbox(repo, tmp_path / "work")
    wt = plant_seed(sandbox, bundle, tmp_path / "work")
    # the seed is applied and committed on eval/e1
    assert (wt / "impl.py").read_text(encoding="utf-8").startswith("def f(x):")
    assert git(wt, "status", "--porcelain") == ""
    # spec exists ONLY inside the sandbox
    assert (wt / TARGET_DIR_NAME / "specs" / "e1.md").is_file()
    assert not (repo / TARGET_DIR_NAME / "specs" / "e1.md").exists()
    # the log records the developer phase done -> loop resumes at fast_tests
    entries = TaskArtifacts(wt, "e1").read_log()
    assert [e["action"] for e in entries] == ["developer"]
    assert derive_resume_point(entries, max_loops=1).phase == "fast_tests"


def test_plant_seed_rejects_non_applying_patch(tmp_path: Path) -> None:
    repo = _repo_with_bundle(tmp_path)
    bundle = load_bundle(repo, "e1")
    sandbox = make_sandbox(repo, tmp_path / "work")
    plant_seed(sandbox, bundle, tmp_path / "work")  # impl.py now exists on eval/e1
    with pytest.raises(EvalBundleError, match="apply"):
        # same worktree: applying the same new-file patch again must fail loudly
        plant_seed(sandbox, bundle, tmp_path / "work")


from orchestrator.oracle.evalrun import EvalTools, run_eval
from orchestrator.oracle.runlog import read_evals, watermark
from tests.fakes import FakeRunner, FakeShell, blocker, verdict_json


def _repo_with_bundle_and_roles(
    tmp_path: Path,
    roles: str = "[developer, rw1]",
    *,
    spec_text: str | None = None,
) -> Path:
    repo = init_repo(tmp_path / "repo")
    seed_registry(repo)
    roles_dir = repo / TARGET_DIR_NAME / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    for name in ("developer", "rw1", "rw2", "senior-reviewer"):
        (roles_dir / f"{name}.md").write_text(
            f"{name.upper()} ROLE\n", encoding="utf-8", newline="\n"
        )
    from tests.test_oracle_evals import SPEC_TEXT

    write_bundle(
        repo, spec_text=spec_text or SPEC_TEXT.replace("[developer, rw1]", roles)
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "bundle + registry + roles")
    return repo


def _tools(rw1: FakeRunner, rw2: FakeRunner | None = None) -> EvalTools:
    # every eval here runs fast_tests exactly once (max_loops=1); queue that
    # one green explicitly - FakeShell fails closed on an unqueued call
    return EvalTools(
        rw1_runner=rw1, rw2_runner=rw2, senior_runner=None,
        shell=FakeShell(results=[(0, "ok")]), docker_gate=None,
    )


def test_run_eval_caught_by_rw1(tmp_path: Path) -> None:
    repo = _repo_with_bundle_and_roles(tmp_path)
    rw1 = FakeRunner([
        verdict_json("CHANGES_REQUESTED", findings=[blocker(file="impl.py")])
    ])
    outcome = run_eval(
        repo, "e1", work_root=tmp_path / "work", tools=_tools(rw1),
        fast_commands="fake-tests", fix_ref="fix123",
    )
    assert outcome.result == "caught"
    assert outcome.caught_by == ("rw1",)
    assert outcome.terminal == "CAP_REACHED"  # blocked seed is never 'fixed'
    # recorded in the REAL repo's run log; watermark untouched
    events = read_evals(repo)
    assert len(events) == 1 and events[0]["fix_ref"] == "fix123"
    assert watermark(repo) is None
    # nothing leaked into the real repo's refs; sandbox is cleaned up
    assert "eval/e1" not in git(repo, "for-each-ref", "--format=%(refname:short)")
    assert not (tmp_path / "work" / "eval-hub.git").exists()


def test_run_eval_missed_when_gates_wave_it_through(tmp_path: Path) -> None:
    repo = _repo_with_bundle_and_roles(tmp_path, roles="[developer, rw1, rw2]")
    rw1 = FakeRunner([verdict_json("APPROVED")])
    rw2 = FakeRunner([verdict_json("APPROVED")])
    outcome = run_eval(
        repo, "e1", work_root=tmp_path / "work", tools=_tools(rw1, rw2),
        fast_commands="fake-tests", keep=True, record=False,
    )
    assert outcome.result == "missed"
    # without the docker gate the decision is stop_before_merge - and the
    # fold must NOT let that masquerade as 'caught'
    assert outcome.decision == "stop_before_merge"
    assert read_evals(repo) == []  # record=False
    # keep=True leaves the sandbox for inspection; the planted bug sits in
    # the HUB's eval/* namespace only
    hub = tmp_path / "work" / "eval-hub.git"
    assert "eval/e1" in git(hub, "for-each-ref", "--format=%(refname:short)")


def test_run_eval_bug_type_composition_measures_gates_not_dev_roles(tmp_path: Path) -> None:
    # A bundle scaffolded from a bug-type task inherits COMPOSITIONS["bug"]
    # (explorer, developer, debugger, rw1, rw2). The seed IS the recorded
    # developer output: dev-side roles must never run inside the eval - their
    # runner is the NeverRunner, so an unfiltered composition dies at
    # INTERNAL_ERROR before any gate and every run folds to inconclusive.
    from tests.test_oracle_evals import SPEC_TEXT

    bug_spec = SPEC_TEXT.replace(
        "type: feature\nroles: [developer, rw1]", "type: bug"
    )
    repo = _repo_with_bundle_and_roles(tmp_path, spec_text=bug_spec)
    rw1 = FakeRunner([
        verdict_json("CHANGES_REQUESTED", findings=[blocker(file="impl.py")])
    ])
    rw2 = FakeRunner([verdict_json("APPROVED")])
    outcome = run_eval(
        repo, "e1", work_root=tmp_path / "work", tools=_tools(rw1, rw2),
        fast_commands="fake-tests", record=False,
    )
    assert outcome.terminal != "INTERNAL_ERROR"
    assert outcome.result == "caught"
    assert outcome.caught_by == ("rw1",)


def test_run_eval_caught_by_red_fast_tests(tmp_path: Path) -> None:
    repo = _repo_with_bundle_and_roles(tmp_path)
    shell = FakeShell(results=[(1, "1 failed")])
    tools = EvalTools(
        rw1_runner=FakeRunner(), rw2_runner=None, senior_runner=None,
        shell=shell, docker_gate=None,
    )
    outcome = run_eval(
        repo, "e1", work_root=tmp_path / "work", tools=tools,
        fast_commands="fake-tests", record=False,
    )
    assert outcome.result == "caught"
    assert outcome.caught_by == ("fast_tests",)
