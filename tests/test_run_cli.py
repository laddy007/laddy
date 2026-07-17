"""Tests for the run.py CLI (injected deps, no real LLM)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.agents import AgentResult
from orchestrator.artifacts import TaskArtifacts
from orchestrator.config import OrchestratorConfig
from orchestrator.gitops import GitOps
from orchestrator.queue import TaskQueue, run_lock
from orchestrator.run import Deps, _derive_status, _parse_selection, main
from tests.fakes import FakeRunner, FakeShell, verdict_json, write_policy_toml


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.com")


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    (seed / TARGET_DIR_NAME / "specs").mkdir(parents=True)
    (seed / TARGET_DIR_NAME / "specs" / "t1.md").write_text("# t1\n", encoding="utf-8")
    (seed / TARGET_DIR_NAME / "roles").mkdir()
    for role in ("developer", "rw1", "rw2", "senior-reviewer", "explorer"):
        (seed / TARGET_DIR_NAME / "roles" / f"{role}.md").write_text(
            f"{role.upper()}\n", encoding="utf-8"
        )
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "init")
    _git("-C", str(seed), "push", "origin", "HEAD:main")
    return bare


def _env(remote: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "AGENT_REPO_URL": remote.as_uri(),
        "AGENT_WORK_ROOT": str(tmp_path / "work"),
    }


def _deps(
    runner_outputs: Sequence[str | AgentResult],
    shell: FakeShell | None = None,
    rw2_outputs: list[str] | None = None,
    ask: Callable[[str], str] | None = None,
) -> Deps:
    runners = FakeRunner(list(runner_outputs))
    rw2 = FakeRunner(list(rw2_outputs or []))
    senior = FakeRunner([])
    the_shell = shell or FakeShell()
    return Deps(
        # one role-keyed resolver now: rw2 -> rw2 fake, senior -> senior fake,
        # everything else (developer/rw1/clarify) -> the shared runners fake.
        make_runner=lambda c, role: (
            rw2 if role == "rw2" else senior if role == "senior" else runners
        ),
        ask=ask or (lambda q: "ANSWER"),
        shell=the_shell,
        # gate_shell defaults to the real containerized shell; in tests the
        # docker gate must use the same fake as the fast gate (no real docker).
        gate_shell=the_shell,
    )


def _push_spec(remote: Path, tmp_path: Path, task_id: str, content: str) -> None:
    """Push <TARGET_DIR_NAME>/specs/<task_id>.md to origin/main via a throwaway clone
    (mirrors _push_draft_spec, generalized to any task id/content)."""
    seed = tmp_path / f"seed-{task_id}"
    _git("clone", str(remote), str(seed))
    (seed / TARGET_DIR_NAME / "specs" / f"{task_id}.md").write_text(content, encoding="utf-8")
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", f"add spec {task_id}")
    _git("-C", str(seed), "push", "origin", "HEAD:main")


def _mark_clarified(env: dict[str, str], task_id: str) -> None:
    """Pre-mark a task's clarify gate as done, the way _phase_clarify does: an
    iteration-log entry with action="clarify" (the exact action name
    has_clarify checks for) on the task's local worktree."""
    config = OrchestratorConfig.from_env(env)
    gitops = GitOps(config.repo_url, config.work_root, config.default_branch)
    wt = gitops.task_worktree(task_id)
    TaskArtifacts(wt, task_id).append_log(action="clarify", outcome="ok")


def test_phase_clarify_runs_gate_and_commits(remote: Path, tmp_path: Path) -> None:
    deps = _deps([json.dumps({"questions": ["Scope?"]})])
    rc = main(["t1", "--phase", "clarify"], env=_env(remote, tmp_path), deps=deps)
    assert rc == 0
    wt = tmp_path / "work" / "wt" / "t1"
    spec = (wt / TARGET_DIR_NAME / "specs" / "t1.md").read_text(encoding="utf-8")
    assert "**A1:** ANSWER" in spec
    # committed on the task branch
    assert "Clarify gate for t1" in _git("-C", str(wt), "log", "--oneline", "-3")


def test_phase_loop_refuses_without_clarify(remote: Path, tmp_path: Path) -> None:
    rc = main(["t1", "--phase", "loop"], env=_env(remote, tmp_path), deps=_deps([]))
    assert rc == 2


def test_phase_loop_skip_clarify_runs_to_push(remote: Path, tmp_path: Path) -> None:
    # default composition (type: feature) = developer, rw1, rw2 + docker gate
    deps = _deps(
        ["dev work done", verdict_json("APPROVED")],
        # one green for fast_tests + one for the (fake-shelled) docker gate
        shell=FakeShell(results=[(0, "ok"), (0, "ok")]),
        rw2_outputs=[verdict_json("APPROVED")],
    )
    rc = main(
        ["t1", "--phase", "loop", "--skip-clarify"],
        env=_env(remote, tmp_path),
        deps=deps,
    )
    assert rc == 0
    assert _git("-C", str(remote), "rev-parse", "refs/heads/t1")


def test_phase_all_chains_clarify_then_loop(remote: Path, tmp_path: Path) -> None:
    deps = _deps(
        [json.dumps({"questions": []}), "dev work", verdict_json("APPROVED")],
        # one green for fast_tests + one for the (fake-shelled) docker gate
        shell=FakeShell(results=[(0, "ok"), (0, "ok")]),
        rw2_outputs=[verdict_json("APPROVED")],
    )
    rc = main(["t1", "--phase", "all"], env=_env(remote, tmp_path), deps=deps)
    assert rc == 0
    wt = tmp_path / "work" / "wt" / "t1"
    log = TaskArtifacts(wt, "t1").read_log()
    assert [e["action"] for e in log] == [
        "clarify", "developer", "fast_tests", "rw1", "rw2", "authoritative", "push",
        "terminal",
    ]
    role_plan = TaskArtifacts(wt, "t1").read_json("role-plan.json")
    assert role_plan == {"task": "t1", "type": "feature", "roles": ["developer", "rw1", "rw2"]}


def _push_draft_spec(remote: Path, tmp_path: Path) -> None:
    seed = tmp_path / "seed2"
    _git("clone", str(remote), str(seed))
    (seed / TARGET_DIR_NAME / "specs" / "draft.md").write_text(
        "---\nstatus: draft-proposal\n---\n# Fix\n", encoding="utf-8"
    )
    write_policy_toml(seed)
    _git("-C", str(seed), "add", "-A")
    _git("-C", str(seed), *IDENTITY, "commit", "-m", "draft spec")
    _git("-C", str(seed), "push", "origin", "HEAD:main")


def test_draft_proposal_spec_is_refused(remote: Path, tmp_path: Path) -> None:
    _push_draft_spec(remote, tmp_path)
    rc = main(["draft", "--phase", "clarify"], env=_env(remote, tmp_path), deps=_deps([]))
    assert rc == 2


def test_draft_proposal_refused_in_loop_phase_with_skip_clarify(
    remote: Path, tmp_path: Path
) -> None:
    # the draft gate must also fire on the loop path, which bypasses clarify
    _push_draft_spec(remote, tmp_path)
    rc = main(
        ["draft", "--phase", "loop", "--skip-clarify"],
        env=_env(remote, tmp_path),
        deps=_deps([]),
    )
    assert rc == 2


def test_clarify_is_idempotent_on_re_kickoff(remote: Path, tmp_path: Path) -> None:
    # first kickoff runs the gate and records it
    deps1 = _deps([json.dumps({"questions": ["Scope?"]})])
    assert main(["t1", "--phase", "clarify"], env=_env(remote, tmp_path), deps=deps1) == 0
    wt = tmp_path / "work" / "wt" / "t1"
    spec_after_first = (wt / TARGET_DIR_NAME / "specs" / "t1.md").read_text(encoding="utf-8")
    assert spec_after_first.count("## Clarifications") == 1

    # re-kickoff (e.g. resume) must NOT re-run the gate or append a 2nd block
    deps2 = Deps(
        make_runner=lambda c, role: FakeRunner([]),  # would raise if the gate re-ran
        ask=lambda q: (_ for _ in ()).throw(AssertionError("ask re-invoked")),
        shell=FakeShell(),
    )
    assert main(["t1", "--phase", "clarify"], env=_env(remote, tmp_path), deps=deps2) == 0
    spec_after_second = (wt / TARGET_DIR_NAME / "specs" / "t1.md").read_text(encoding="utf-8")
    assert spec_after_second.count("## Clarifications") == 1


def test_missing_spec_exits_2(remote: Path, tmp_path: Path) -> None:
    rc = main(["nope", "--phase", "clarify"], env=_env(remote, tmp_path), deps=_deps([]))
    assert rc == 2


def test_new_mode_authors_spec_and_pushes(remote: Path, tmp_path: Path) -> None:
    # a task whose spec does NOT exist on main; --new authors it interactively
    def author(wt: Path, task_id: str, spec_rel: str) -> None:
        (wt / spec_rel).write_text("---\ntype: feature\n---\n# Authored\n", encoding="utf-8")

    deps = Deps(
        make_runner=lambda c, role: FakeRunner([]),
        author_spec=author,
        ask=lambda q: "ANSWER",
        shell=FakeShell(),
    )
    rc = main(["freshtask", "--phase", "new"], env=_env(remote, tmp_path), deps=deps)
    assert rc == 0
    # the authored spec is pushed on the branch
    assert (
        _git(
            "-C",
            str(remote),
            "cat-file",
            "-e",
            f"freshtask:{TARGET_DIR_NAME}/specs/freshtask.md",
        )
        == ""
    )


def test_new_mode_refuses_when_spec_already_exists(remote: Path, tmp_path: Path) -> None:
    # t1 spec already exists on main (from the fixture); --new must refuse
    called = False

    def author(wt: Path, task_id: str, spec_rel: str) -> None:
        nonlocal called
        called = True

    deps = Deps(make_runner=lambda c, role: FakeRunner([]), author_spec=author)
    rc = main(["t1", "--phase", "new"], env=_env(remote, tmp_path), deps=deps)
    assert rc == 2
    assert called is False  # never overwrites an existing spec


def test_new_mode_errors_if_no_spec_produced(remote: Path, tmp_path: Path) -> None:
    deps = Deps(
        make_runner=lambda c, role: FakeRunner([]),
        author_spec=lambda wt, task, spec_rel: None,  # produces nothing
    )
    rc = main(["freshtask", "--phase", "new"], env=_env(remote, tmp_path), deps=deps)
    assert rc == 2


def test_gitops_from_config(remote: Path, tmp_path: Path) -> None:
    deps = Deps()
    from orchestrator.config import OrchestratorConfig

    config = OrchestratorConfig.from_env(_env(remote, tmp_path))
    gitops = deps.make_gitops(config)
    assert isinstance(gitops, GitOps)
    assert gitops.repo_url == remote.as_uri()


# --- enqueue / queue / queue-list (spec: quota-resume-queue Task 6) ---------


def test_enqueue_refuses_task_without_clarify(remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    rc = main(["qt1", "--phase", "enqueue"], env=env, deps=_deps([]))
    assert rc == 2
    assert TaskQueue(Path(env["AGENT_WORK_ROOT"])).items() == []


def test_enqueue_accepts_clarified_task(remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    rc = main(["qt1", "--phase", "enqueue"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == ["qt1"]


def test_enqueue_with_skip_clarify_flag_accepts_unclarified(
    remote: Path, tmp_path: Path
) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    rc = main(["qt1", "--phase", "enqueue", "--skip-clarify"], env=env, deps=_deps([]))
    assert rc == 0
    assert TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()[0].skip_clarify is True


def test_enqueue_refuses_draft_spec(remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "---\nstatus: draft-proposal\n---\n# qt1\n")
    env = _env(remote, tmp_path)
    rc = main(["qt1", "--phase", "enqueue"], env=env, deps=_deps([]))
    assert rc == 2


def test_enqueue_accepts_done_spec_deliberate_rerun(remote: Path, tmp_path: Path) -> None:
    # a status: done spec IS allowed here (deliberate re-run) unlike --all/--pick
    _push_spec(remote, tmp_path, "qt1", "---\nstatus: done\n---\n# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    rc = main(["qt1", "--phase", "enqueue"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == ["qt1"]


def test_enqueue_many_tasks_fifo(remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    _push_spec(remote, tmp_path, "qt2", "# qt2\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    _mark_clarified(env, "qt2")
    rc = main(["qt1", "qt2", "--phase", "enqueue"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == [
        "qt1",
        "qt2",
    ]


def test_enqueue_many_is_all_or_nothing(remote: Path, tmp_path: Path) -> None:
    # qt1 clarified, qt2 has no spec at all -> nothing gets queued
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    rc = main(["qt1", "qt2", "--phase", "enqueue"], env=env, deps=_deps([]))
    assert rc == 2
    assert TaskQueue(Path(env["AGENT_WORK_ROOT"])).items() == []


def test_enqueue_same_task_twice_in_one_call_queues_nothing(
    remote: Path, tmp_path: Path
) -> None:
    # qt1 clarified; passing it twice is a validation error, not a partial
    # enqueue -> rc 2 and an EMPTY queue (regression guard: an unguarded
    # second queue.enqueue() would raise QueueError after writing the first).
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    rc = main(["qt1", "qt1", "--phase", "enqueue"], env=env, deps=_deps([]))
    assert rc == 2
    assert TaskQueue(Path(env["AGENT_WORK_ROOT"])).items() == []


def test_enqueue_all_queues_candidates_and_skips_unready(
    remote: Path, tmp_path: Path
) -> None:
    # qt1 (clarified), qt2 (NOT clarified), qt3 (status: done),
    # qt4 (status: draft-proposal) -> only qt1 queued, qt2 warned+skipped
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    _push_spec(remote, tmp_path, "qt2", "# qt2\n")
    _push_spec(remote, tmp_path, "qt3", "---\nstatus: done\n---\n# qt3\n")
    _push_spec(remote, tmp_path, "qt4", "---\nstatus: draft-proposal\n---\n# qt4\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    rc = main(["--phase", "enqueue", "--all"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == ["qt1"]


def test_enqueue_all_skips_high_risk_without_design_approval(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    _push_spec(remote, tmp_path, "hr",
               "---\ntype: feature\nrisk: high\n---\n# t\nTouch .laddy/orchestrator/run.py.\n")
    _mark_clarified(env, "hr")
    rc = main(["--phase", "enqueue", "--all"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == []


def test_enqueue_all_queues_high_risk_after_design_approval(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    _push_spec(remote, tmp_path, "hr",
               "---\ntype: feature\nrisk: high\n---\n# t\nTouch .laddy/orchestrator/run.py.\n")
    _mark_clarified(env, "hr")
    config = OrchestratorConfig.from_env(env)
    gitops = GitOps(config.repo_url, config.work_root, config.default_branch)
    wt = gitops.task_worktree("hr")
    TaskArtifacts(wt, "hr").append_log(action="design", outcome="approved")
    rc = main(["--phase", "enqueue", "--all"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == ["hr"]


def test_enqueue_pick_queues_selected(remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    _push_spec(remote, tmp_path, "qt2", "# qt2\n")
    _push_spec(remote, tmp_path, "qt3", "# qt3\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    _mark_clarified(env, "qt2")
    _mark_clarified(env, "qt3")
    deps = _deps([], ask=lambda q: "1 3")
    rc = main(["--phase", "enqueue", "--pick"], env=env, deps=deps)
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == [
        "qt1",
        "qt3",
    ]


def test_enqueue_pick_empty_input_queues_nothing(remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    deps = _deps([], ask=lambda q: "")
    rc = main(["--phase", "enqueue", "--pick"], env=env, deps=deps)
    assert rc == 0
    assert TaskQueue(Path(env["AGENT_WORK_ROOT"])).items() == []


def test_enqueue_modes_are_mutually_exclusive(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    with pytest.raises(SystemExit):  # argparse error
        main(["qt1", "--phase", "enqueue", "--all"], env=env, deps=_deps([]))
    with pytest.raises(SystemExit):
        main(["--phase", "enqueue", "--all", "--pick"], env=env, deps=_deps([]))


def test_queue_processes_fifo_and_removes_after_terminal(
    remote: Path, tmp_path: Path
) -> None:
    # enqueue qt1 + qt2 (clarified); qt1's developer round errors out and (with
    # MAX_LOOPS=1) hits the round cap -> CAP_REACHED (rc 1); qt2 succeeds to
    # push (rc 0) - both must still be processed and removed from the queue.
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    _push_spec(remote, tmp_path, "qt2", "# qt2\n")
    env = _env(remote, tmp_path)
    env["MAX_LOOPS"] = "1"
    _mark_clarified(env, "qt1")
    _mark_clarified(env, "qt2")
    assert main(["qt1", "--phase", "enqueue"], env=env, deps=_deps([])) == 0
    assert main(["qt2", "--phase", "enqueue"], env=env, deps=_deps([])) == 0

    deps = _deps(
        [
            AgentResult(text="boom", session_id="s1", exit_reason="error", returncode=1),
            "dev work done",
            verdict_json("APPROVED"),
        ],
        rw2_outputs=[verdict_json("APPROVED")],
    )
    rc = main(["--phase", "queue"], env=env, deps=deps)
    assert rc == 0
    assert TaskQueue(Path(env["AGENT_WORK_ROOT"])).items() == []


def test_queue_refuses_when_locked(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    q = TaskQueue(Path(env["AGENT_WORK_ROOT"]))
    q.dir.mkdir(parents=True, exist_ok=True)
    (q.dir / ".lock").write_text("999\n", encoding="utf-8")
    rc = main(["--phase", "queue"], env=env, deps=_deps([]))
    assert rc == 3


def test_queue_list_prints_items(capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    assert main(["qt1", "--phase", "enqueue"], env=env, deps=_deps([])) == 0
    rc = main(["--phase", "queue-list"], env=env, deps=_deps([]))
    assert rc == 0
    assert "qt1" in capsys.readouterr().out


def test_queue_list_empty_prints_nothing_bad(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    env = _env(remote, tmp_path)
    rc = main(["--phase", "queue-list"], env=env, deps=_deps([]))
    assert rc == 0
    assert "empty" in capsys.readouterr().out


def test_queue_takes_no_task_id(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    with pytest.raises(SystemExit):
        main(["qt1", "--phase", "queue"], env=env, deps=_deps([]))


def test_existing_phases_require_exactly_one_task_id(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    with pytest.raises(SystemExit):
        main(["--phase", "clarify"], env=env, deps=_deps([]))
    with pytest.raises(SystemExit):
        main(["t1", "t2", "--phase", "clarify"], env=env, deps=_deps([]))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("1", [1]),
        ("1 3", [1, 3]),
        ("1,3", [1, 3]),
        ("1 3-5", [1, 3, 4, 5]),
        ("2-2", [2]),
    ],
)
def test_parse_selection(raw: str, expected: list[int]) -> None:
    assert _parse_selection(raw, count=6) == expected


def test_parse_selection_rejects_out_of_range_and_garbage() -> None:
    with pytest.raises(ValueError):
        _parse_selection("7", count=6)
    with pytest.raises(ValueError):
        _parse_selection("abc", count=6)
    with pytest.raises(ValueError):
        _parse_selection("5-3", count=6)


# --- derived status + per-task run lock (spec: quota-resume-queue Task 7) --


def test_second_loop_on_locked_task_exits_4(remote: Path, tmp_path: Path) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    work_root = Path(env["AGENT_WORK_ROOT"])
    with run_lock(work_root, "qt1"):
        rc = main(
            ["qt1", "--phase", "loop", "--skip-clarify"], env=env, deps=_deps([])
        )
    assert rc == 4


def test_queue_keeps_lock_refused_item_with_warning(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    # A task whose per-task run lock is held by a concurrent direct run was
    # SKIPPED, not run to a terminal: it must stay in the queue (removing it
    # loses the task if that other run later fails), with a warning.
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    work_root = Path(env["AGENT_WORK_ROOT"])
    assert main(["qt1", "--phase", "enqueue"], env=env, deps=_deps([])) == 0
    with run_lock(work_root, "qt1"):
        rc = main(["--phase", "queue"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(work_root).items()] == ["qt1"]  # left in queue
    assert "WARNING" in capsys.readouterr().out


def test_derive_status_order(tmp_path: Path) -> None:
    """Table test over synthetic spec files + artifacts; checks the exact
    precedence: unparseable > draft/done > running > queued > failed >
    pushed > in-progress > ready."""
    specs = tmp_path / "specs"
    specs.mkdir(parents=True)
    work_root = tmp_path / "work"

    def spec(name: str, body: str = "# t\n") -> Path:
        p = specs / f"{name}.md"
        p.write_text(body, encoding="utf-8")
        return p

    def log(task: str, *entries: dict[str, object]) -> None:
        # write iteration-log.jsonl into the LOCAL worktree layout
        # <work_root>/wt/<task>/<TARGET_DIR_NAME>/tasks/<task>/
        d = work_root / "wt" / task / TARGET_DIR_NAME / "tasks" / task
        d.mkdir(parents=True)
        lines = [json.dumps({"ts": "t", **e}) for e in entries]
        (d / "iteration-log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert _derive_status(spec("bad", "---\nbroken\n"), work_root, set()) == "unparseable"
    assert (
        _derive_status(spec("d", "---\nstatus: draft-proposal\n---\n# t\n"), work_root, set())
        == "draft"
    )
    assert (
        _derive_status(spec("done", "---\nstatus: done\n---\n# t\n"), work_root, set())
        == "done"
    )
    running = spec("run")
    (work_root / "locks").mkdir(parents=True)
    (work_root / "locks" / "run.lock").write_text("1\n", encoding="utf-8")
    assert _derive_status(running, work_root, set()) == "running"
    assert _derive_status(spec("q"), work_root, {"q"}) == "queued"
    log("f", {"action": "developer", "outcome": "ok"}, {"action": "terminal", "outcome": "CAP_REACHED"})
    assert _derive_status(spec("f"), work_root, set()) == "failed:CAP_REACHED"
    log("p", {"action": "developer", "outcome": "ok"}, {"action": "push", "outcome": "ok"})
    assert _derive_status(spec("p"), work_root, set()) == "pushed"
    # success-kind terminal markers (written by the unified terminal tail)
    # read as pushed, not failed:<state>
    log("pt", {"action": "push", "outcome": "ok"}, {"action": "terminal", "outcome": "PUSHED"})
    assert _derive_status(spec("pt"), work_root, set()) == "pushed"
    log(
        "md",
        {"action": "push", "outcome": "ok"},
        {"action": "terminal", "outcome": "MERGE_DECIDED:auto_merge"},
    )
    assert _derive_status(spec("md"), work_root, set()) == "pushed"
    log("ip", {"action": "developer", "outcome": "ok"})
    assert _derive_status(spec("ip"), work_root, set()) == "in-progress"
    assert _derive_status(spec("r"), work_root, set()) == "ready"
    # a task carrying ONLY flag events (no progress action) is still "ready":
    # raising a flag must not falsely mark a ready task as in-progress
    log(
        "flg",
        {"action": "clarify", "outcome": "ok"},
        {"action": "flag", "id": "flg#1", "kind": "note", "summary": "x",
         "needs_director": False},
        {"action": "flag-resolved", "id": "flg#1", "resolution": "resolved"},
    )
    assert _derive_status(spec("flg"), work_root, set()) == "ready"
    # a task WITH a progress action stays in-progress even when it carries flags
    log(
        "ipf",
        {"action": "developer", "outcome": "ok"},
        {"action": "flag", "id": "ipf#1", "kind": "blocker", "summary": "y",
         "needs_director": True},
    )
    assert _derive_status(spec("ipf"), work_root, set()) == "in-progress"


def test_deps_gate_shell_defaults_to_signal_preserving_shell() -> None:
    # The authoritative gate must run under the stderr-first/stdout-last shell
    # so the pytest result survives into output_tail; run.py wires DockerGate
    # to deps.gate_shell, so this default is load-bearing (flow-audit finding).
    from orchestrator.testgate import _subprocess_shell_gate

    assert Deps().gate_shell is _subprocess_shell_gate


def test_derive_status_progress_actions_beyond_phase_actions(tmp_path: Path) -> None:
    """A task interrupted after only explorer / investigator / verify /
    quota_* / path_guard entries (progress actions the loop writes that are
    NOT in _PHASE_ACTIONS) must read 'in-progress', not 'ready' - otherwise
    enqueue --all silently re-queues a half-started task."""
    specs = tmp_path / "specs"
    specs.mkdir(parents=True)
    work_root = tmp_path / "work"

    def spec(name: str) -> Path:
        p = specs / f"{name}.md"
        p.write_text("# t\n", encoding="utf-8")
        return p

    def log(task: str, *entries: dict[str, object]) -> None:
        d = work_root / "wt" / task / TARGET_DIR_NAME / "tasks" / task
        d.mkdir(parents=True)
        lines = [json.dumps({"ts": "t", **e}) for e in entries]
        (d / "iteration-log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    for action in ("explorer", "investigator", "verify", "quota_exhausted", "path_guard"):
        log(action, {"action": "clarify", "outcome": "ok"}, {"action": action, "outcome": "ok"})
        assert _derive_status(spec(action), work_root, set()) == "in-progress", action


def test_derive_status_clarify_and_design_is_ready(tmp_path: Path) -> None:
    """A task that has only clarify and design log entries (both non-progress
    actions) must read 'ready', not 'in-progress' — design is a flag/gate
    event like clarify, not a progress milestone."""
    specs = tmp_path / "specs"
    specs.mkdir(parents=True)
    work_root = tmp_path / "work"

    def spec(name: str) -> Path:
        p = specs / f"{name}.md"
        p.write_text("# t\n", encoding="utf-8")
        return p

    def log(task: str, *entries: dict[str, object]) -> None:
        d = work_root / "wt" / task / TARGET_DIR_NAME / "tasks" / task
        d.mkdir(parents=True)
        lines = [json.dumps({"ts": "t", **e}) for e in entries]
        (d / "iteration-log.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log("design_ready", {"action": "clarify", "outcome": "ok"}, {"action": "design", "outcome": "approved"})
    assert _derive_status(spec("design_ready"), work_root, set()) == "ready"


def test_phase_status_lists_all_specs(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    env = _env(remote, tmp_path)
    rc = main(["--phase", "status"], env=env, deps=_deps([]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "t1" in out  # seeded by the remote fixture
    assert "qt1" in out


def test_pick_offers_in_progress_marked_all_does_not(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    _push_spec(remote, tmp_path, "qt1", "# qt1\n")
    _push_spec(remote, tmp_path, "qt2", "# qt2\n")
    env = _env(remote, tmp_path)
    _mark_clarified(env, "qt1")
    _mark_clarified(env, "qt2")
    # qt2 has a non-empty log -> in-progress
    config = OrchestratorConfig.from_env(env)
    gitops = GitOps(config.repo_url, config.work_root, config.default_branch)
    wt2 = gitops.task_worktree("qt2")
    TaskArtifacts(wt2, "qt2").append_log(action="developer", outcome="ok")

    rc = main(["--phase", "enqueue", "--all"], env=env, deps=_deps([]))
    assert rc == 0
    assert [i.task_id for i in TaskQueue(Path(env["AGENT_WORK_ROOT"])).items()] == ["qt1"]

    rc2 = main(["--phase", "enqueue", "--pick"], env=env, deps=_deps([], ask=lambda q: ""))
    assert rc2 == 0
    out = capsys.readouterr().out
    assert "qt2" in out
    assert "[in-progress]" in out


# --- flag / flags CLI --------------------------------------------------------


def _task_log(env: dict[str, str], task_id: str) -> list[dict[str, object]]:
    wt = Path(env["AGENT_WORK_ROOT"]) / "wt" / task_id
    return TaskArtifacts(wt, task_id).read_log()


def test_flag_raise_prints_id_and_writes_event(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    env = _env(remote, tmp_path)
    rc = main(
        ["t1", "--phase", "flag", "--kind", "deviation", "--summary", "stricter regex",
         "--needs-director", "--detail", "vs AC2", "--round", "2"],
        env=env, deps=_deps([]),
    )
    assert rc == 0
    assert "t1#1" in capsys.readouterr().out
    [event] = [e for e in _task_log(env, "t1") if e.get("action") == "flag"]
    assert event["id"] == "t1#1"
    assert event["kind"] == "deviation"
    assert event["summary"] == "stricter regex"
    assert event["needs_director"] is True
    assert event["detail"] == "vs AC2"
    assert event["round"] == 2


def test_flag_bad_kind_exits_2(remote: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["t1", "--phase", "flag", "--kind", "nope", "--summary", "x"],
             env=_env(remote, tmp_path), deps=_deps([]))
    assert exc.value.code == 2


def test_flag_kind_oracle_escape_rejected_at_loop_cli(remote: Path, tmp_path: Path) -> None:
    # oracle-escape enters only through the validated Director channel
    # (orchestrator.oracle escape); the loop CLI raising it would bypass
    # every validation and pollute the escape ledger.
    with pytest.raises(SystemExit) as exc:
        main(["t1", "--phase", "flag", "--kind", "oracle-escape", "--summary", "x"],
             env=_env(remote, tmp_path), deps=_deps([]))
    assert exc.value.code == 2


def test_flag_resolve_oracle_escape_refused_at_loop_cli(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    # Dismissing an oracle-escape drops it from the ledger - Director only.
    env = _env(remote, tmp_path)
    main(["t1", "--phase", "flag", "--kind", "note", "--summary", "seed log"],
         env=env, deps=_deps([]))
    wt = Path(env["AGENT_WORK_ROOT"]) / "wt" / "t1"
    from orchestrator.flags import ORACLE_ESCAPE, raise_flag

    fid = raise_flag(TaskArtifacts(wt, "t1"), ORACLE_ESCAPE, "esc",
                     needs_director=True)
    rc = main(["t1", "--phase", "flag", "--resolve", fid,
               "--resolution", "dismissed"], env=env, deps=_deps([]))
    assert rc == 2
    assert "oracle" in capsys.readouterr().err
    assert not any(
        e.get("action") == "flag-resolved" for e in _task_log(env, "t1")
    )


def test_flag_empty_summary_exits_2(remote: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["t1", "--phase", "flag", "--kind", "note", "--summary", "   "],
             env=_env(remote, tmp_path), deps=_deps([]))
    assert exc.value.code == 2


def test_flag_raise_and_resolve_are_mutually_exclusive(remote: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["t1", "--phase", "flag", "--resolve", "t1#1", "--kind", "note",
              "--summary", "x"], env=_env(remote, tmp_path), deps=_deps([]))
    assert exc.value.code == 2


def test_flag_resolve_unknown_id_exits_3_writes_nothing(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    env = _env(remote, tmp_path)
    rc = main(["t1", "--phase", "flag", "--resolve", "t1#99"], env=env, deps=_deps([]))
    assert rc == 3
    assert not any(e.get("action") == "flag-resolved" for e in _task_log(env, "t1"))


def test_flag_resolve_open_flag_succeeds(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    main(["t1", "--phase", "flag", "--kind", "question", "--summary", "why?"],
         env=env, deps=_deps([]))
    rc = main(["t1", "--phase", "flag", "--resolve", "t1#1", "--resolution", "dismissed",
               "--note", "moot"], env=env, deps=_deps([]))
    assert rc == 0
    resolved = [e for e in _task_log(env, "t1") if e.get("action") == "flag-resolved"]
    assert resolved and resolved[0]["resolution"] == "dismissed" and resolved[0]["note"] == "moot"


def test_flags_reporter_lists_open_only_needs_director_first(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    env = _env(remote, tmp_path)
    main(["t1", "--phase", "flag", "--kind", "note", "--summary", "minor"],
         env=env, deps=_deps([]))
    main(["t1", "--phase", "flag", "--kind", "blocker", "--summary", "big one",
          "--needs-director"], env=env, deps=_deps([]))
    main(["t1", "--phase", "flag", "--kind", "debt", "--summary", "gone"],
         env=env, deps=_deps([]))
    main(["t1", "--phase", "flag", "--resolve", "t1#3"], env=env, deps=_deps([]))
    capsys.readouterr()  # drop raise/resolve output

    rc = main(["t1", "--phase", "flags"], env=env, deps=_deps([]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "t1: 2 open (1 needs-director)" in out
    assert out.index("big one") < out.index("minor")  # needs-director first
    assert "gone" not in out  # resolved excluded


def test_flags_reporter_empty_says_no_open_flags(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    rc = main(["t1", "--phase", "flags"], env=_env(remote, tmp_path), deps=_deps([]))
    assert rc == 0
    assert "no open flags" in capsys.readouterr().out


def test_flag_unknown_task_id_is_rejected_and_creates_no_worktree(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    # A typo'd task id (no spec) must be rejected, not silently create a junk
    # worktree/branch that strands the flag where no reporter can find it.
    env = _env(remote, tmp_path)
    rc = main(["tsak-1", "--phase", "flag", "--kind", "blocker", "--summary", "db down"],
              env=env, deps=_deps([]))
    assert rc == 2
    assert not (Path(env["AGENT_WORK_ROOT"]) / "wt" / "tsak-1").exists()


def test_flag_note_in_raise_mode_is_rejected(remote: Path, tmp_path: Path) -> None:
    # --note is the resolution note (resolve mode); in raise mode it was
    # silently dropped. It must now be rejected rather than lost.
    with pytest.raises(SystemExit) as exc:
        main(["t1", "--phase", "flag", "--kind", "debt", "--summary", "x",
              "--note", "will fix later"], env=_env(remote, tmp_path), deps=_deps([]))
    assert exc.value.code == 2


def test_flags_reporter_no_args_reads_local_worktrees(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    # The no-arg reporter must enumerate node-local worktrees (offline, no
    # network), not the remote spec list - a flag on a task present only as a
    # local worktree still has to surface.
    env = _env(remote, tmp_path)
    wt = Path(env["AGENT_WORK_ROOT"]) / "wt" / "orphan"
    TaskArtifacts(wt, "orphan").append_log(
        action="flag", id="orphan#1", kind="blocker", summary="stranded",
        needs_director=True,
    )
    rc = main(["--phase", "flags"], env=env, deps=_deps([]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "orphan: 1 open (1 needs-director)" in out
    assert "stranded" in out


# --- design phase gate --------------------------------------------------------


def _seed_high_risk_spec(remote: Path, tmp_path: Path, task_id: str) -> None:
    _push_spec(remote, tmp_path, task_id,
               "---\ntype: feature\nrisk: high\nroles: [explorer, developer, rw1]\n---\n"
               "# t\nTouch `.laddy/orchestrator/run.py`.\n")


def test_design_phase_noop_for_non_high_risk(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    # t1 (seeded by the remote fixture) is benign -> design is a no-op, exit 0
    rc = main(["t1", "--phase", "design"], env=_env(remote, tmp_path), deps=_deps([]))
    assert rc == 0
    assert "not high-risk" in capsys.readouterr().out


def test_design_phase_approve_records_marker(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    _seed_high_risk_spec(remote, tmp_path, "hr1")
    deps = _deps(["EXPLORATION OUTPUT"], ask=lambda q: "approve")
    rc = main(["hr1", "--phase", "design"], env=env, deps=deps)
    assert rc == 0
    log = _task_log(env, "hr1")
    assert any(e.get("action") == "design" and e.get("outcome") == "approved" for e in log)


def test_design_phase_reject_records_and_nonzero(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    _seed_high_risk_spec(remote, tmp_path, "hr2")
    deps = _deps(["EXPLORATION OUTPUT"], ask=lambda q: "no, the approach is unsafe")
    rc = main(["hr2", "--phase", "design"], env=env, deps=deps)
    assert rc == 5
    log = _task_log(env, "hr2")
    assert any(e.get("action") == "design" and e.get("outcome") == "rejected" for e in log)


def test_loop_refuses_high_risk_without_design_approval(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    _seed_high_risk_spec(remote, tmp_path, "hr3")
    _mark_clarified(env, "hr3")
    rc = main(["hr3", "--phase", "loop", "--skip-clarify"], env=env, deps=_deps([]))
    assert rc == 2  # refused: high-risk, no design/approved marker


# --- director resume channel (--phase resume) --------------------------------


def _seed_terminal(env: dict[str, str], task_id: str, state: str) -> None:
    """Give a task a recorded terminal, as a finished run leaves it."""
    config = OrchestratorConfig.from_env(env)
    gitops = GitOps(config.repo_url, config.work_root, config.default_branch)
    wt = gitops.task_worktree(task_id)
    art = TaskArtifacts(wt, task_id)
    art.append_log(action="developer", outcome="ok", round=1)
    art.append_log(action="terminal", outcome=state)


def _resume_events(env: dict[str, str], task_id: str) -> list[dict[str, object]]:
    return [e for e in _task_log(env, task_id) if e.get("action") == "director_resume"]


def test_resume_empty_reason_exits_2_writes_nothing(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    _seed_terminal(env, "t1", "CAP_REACHED")
    rc = main(["t1", "--phase", "resume", "--reason", "   "], env=env, deps=_deps([]))
    assert rc == 2
    assert _resume_events(env, "t1") == []


def test_resume_missing_reason_exits_2_writes_nothing(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    _seed_terminal(env, "t1", "CAP_REACHED")
    rc = main(["t1", "--phase", "resume"], env=env, deps=_deps([]))
    assert rc == 2
    assert _resume_events(env, "t1") == []


def test_resume_refuses_path_guard_violation_writes_nothing(
    remote: Path, tmp_path: Path
) -> None:
    env = _env(remote, tmp_path)
    _seed_terminal(env, "t1", "PATH_GUARD_VIOLATION")
    rc = main(["t1", "--phase", "resume", "--reason", "fix it"], env=env, deps=_deps([]))
    assert rc == 2
    assert _resume_events(env, "t1") == []


def test_resume_refuses_retryable_terminal_with_rekickoff_hint(
    capsys: pytest.CaptureFixture[str], remote: Path, tmp_path: Path
) -> None:
    # a transient/retryable terminal (QUOTA_TIMEOUT) is NOT poisoned - it already
    # resumes on a plain re-kickoff, so the refusal must say that, not "discard".
    env = _env(remote, tmp_path)
    _seed_terminal(env, "t1", "QUOTA_TIMEOUT")
    rc = main(["t1", "--phase", "resume", "--reason", "go"], env=env, deps=_deps([]))
    assert rc == 2
    assert _resume_events(env, "t1") == []
    err = capsys.readouterr().err
    assert "re-kickoff" in err and "poisoned" not in err


def test_resume_refuses_task_with_no_terminal_writes_nothing(
    remote: Path, tmp_path: Path
) -> None:
    # t1 exists on main but never ran (no terminal) -> refuse, nothing written
    env = _env(remote, tmp_path)
    rc = main(["t1", "--phase", "resume", "--reason", "go"], env=env, deps=_deps([]))
    assert rc == 2
    assert _resume_events(env, "t1") == []


def test_resume_unknown_task_exits_2_writes_nothing(remote: Path, tmp_path: Path) -> None:
    env = _env(remote, tmp_path)
    rc = main(["nope", "--phase", "resume", "--reason", "go"], env=env, deps=_deps([]))
    assert rc == 2
    assert _resume_events(env, "nope") == []


def test_resume_happy_path_appends_one_event_and_starts_loop(
    monkeypatch: pytest.MonkeyPatch, remote: Path, tmp_path: Path
) -> None:
    env = _env(remote, tmp_path)
    _seed_terminal(env, "t1", "CAP_REACHED")

    # stub the loop so the test asserts the append + that the loop is entered
    # with clarify skipped, without running a full round.
    from orchestrator import run as run_mod

    calls: list[tuple[str, bool]] = []

    def fake_loop(config: object, task_id: str, deps: object, skip_clarify: bool) -> int:
        calls.append((task_id, skip_clarify))
        return 0

    monkeypatch.setattr(run_mod, "_phase_loop", fake_loop)
    rc = main(["t1", "--phase", "resume", "--reason", "added throttling"],
              env=env, deps=_deps([]))
    assert rc == 0
    events = _resume_events(env, "t1")
    assert len(events) == 1
    assert events[0]["reason"] == "added throttling"
    assert events[0]["outcome"] == "ok"
    assert events[0].get("spec_sha")  # a recorded receipt sha is present
    assert calls == [("t1", True)]  # loop entered, clarify skipped


def test_resume_path_does_not_push_or_merge_or_skip_review(remote: Path, tmp_path: Path) -> None:
    # AC11 (trust): the resume CLI itself un-sticks + notes only. It never pushes
    # to origin, decides a merge, or bypasses a reviewer - the resumed loop
    # re-traverses every gate. Guard the source so a future edit can't sneak a
    # push/merge onto this path.
    import inspect

    from orchestrator.run import _phase_resume

    src = inspect.getsource(_phase_resume)
    assert ".push(" not in src
    assert "merge_decision" not in src and "MERGE_DECISION" not in src
    assert "code_sha" not in src
    # it delegates to the normal loop (which re-runs rw1/rw2/authoritative)
    assert "_phase_loop(" in src
