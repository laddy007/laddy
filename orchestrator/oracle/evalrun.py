"""Sandboxed seeded-eval run: merge of the planted bug is impossible by CODE.

Two independent structural layers (never prompt text):
  1. the loop's "origin" inside the eval is a throwaway LOCAL bare hub
     cloned from the repo - the sandbox never learns a real remote, so
     nothing it pushes can reach GitHub, the local main, or local_merge's
     discover_ready/merge_branch (which only ever look at the Director's
     configured branch_remote, never this sandbox hub). Branches still
     live under eval/* here (EvalGitOps._branch) as a naming convention,
     but that prefix is no longer what keeps them out of local_merge -
     the hub being a distinct, throwaway repo is;
  2. the eval spec exists only inside the sandbox worktree - it is never
     written under the real repo's <agent-dir>/specs/, so the task queue
     can never pick it up.

The developer role is a NeverRunner: the seed IS the developer output.
The gates must review the planted defect exactly as committed - an agent
must never get a chance to quietly fix it before review (run_eval's
max_loops=1 turns every send-back-to-dev into CAP_REACHED instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator import TARGET_DIR_NAME
from orchestrator.artifacts import (
    MERGE_DECISION,
    RW1_VERDICT,
    RW2_VERDICT,
    SENIOR_VERDICT,
    TaskArtifacts,
)
from orchestrator.fsutil import remove_tree
from orchestrator.gitops import GitOps
from orchestrator.handoff import NtfyNotifier
from orchestrator.loop import Orchestrator
from orchestrator.oracle import run_git
from orchestrator.oracle.evals import (
    EvalBundle,
    EvalBundleError,
    EvalOutcome,
    fold_outcome,
    load_bundle,
)
from orchestrator.oracle.runlog import append_eval
from orchestrator.run import default_roles_dir

if TYPE_CHECKING:
    from orchestrator.agents import AgentResult, AgentRunner
    from orchestrator.config import OrchestratorConfig
    from orchestrator.testgate import DockerGate, ShellRunner


class NeverRunner:
    """Fails loudly if the eval ever tries to run the developer role."""

    name = "never"

    def run(self, prompt: str, cwd: Path, resume: str | None = None) -> AgentResult:
        raise RuntimeError(
            "seeded eval: the developer role must never run - the seed is "
            "the developer output"
        )


class EvalGitOps(GitOps):
    """GitOps whose branches live in eval/* (naming convention only - the
    real merge ban is the throwaway sandbox hub, layer 1 above)."""

    def _branch(self, task_id: str) -> str:
        return f"eval/{task_id}"


@dataclass(frozen=True)
class Sandbox:
    hub: Path
    gitops: EvalGitOps


def make_sandbox(repo_root: Path, work_root: Path, base: str = "main") -> Sandbox:
    """Fresh sandbox: bare LOCAL hub cloned from ``repo_root`` (layer 1).

    ``base`` is the BRANCH the eval branches from (default main) - pass a
    fix branch to validate a candidate fix before it ships. The hub is a
    full clone, so every local branch of the repo exists in it.
    """
    hub = work_root / "eval-hub.git"
    for stale in (hub, work_root / "base", work_root / "wt"):
        remove_tree(stale)
    work_root.mkdir(parents=True, exist_ok=True)
    run_git(repo_root, "clone", "--bare", ".", str(hub))
    gitops = EvalGitOps(
        repo_url=str(hub), work_root=work_root, default_branch=base
    )
    return Sandbox(hub=hub, gitops=gitops)


def cleanup_sandbox(work_root: Path) -> None:
    """Remove everything an eval run materialized. The eval work root is
    DEDICATED (default <repo-parent>/myapp-eval-work), never a shared
    orchestrator work root - this only touches the sandbox's own dirs."""
    for name in ("eval-hub.git", "base", "wt"):
        remove_tree(work_root / name)
    for patch in work_root.glob("seed-*.patch"):
        patch.unlink(missing_ok=True)


def plant_seed(sandbox: Sandbox, bundle: EvalBundle, work_root: Path) -> Path:
    """Materialize the eval branch: spec + seeded defect committed as the
    recorded developer output, so the production loop resumes at fast_tests
    and the gates review the seed EXACTLY as planted."""
    wt = sandbox.gitops.task_worktree(bundle.eval_id)
    spec_path = wt / TARGET_DIR_NAME / "specs" / f"{bundle.eval_id}.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(bundle.spec_text, encoding="utf-8", newline="\n")
    patch = work_root / f"seed-{bundle.eval_id}.patch"
    patch.write_text(bundle.seed_patch, encoding="utf-8", newline="\n")
    code, _ = run_git(wt, "apply", str(patch), check=False)
    if code != 0:
        raise EvalBundleError(
            f"{bundle.eval_id}: seed.patch does not apply on the eval branch "
            "(run eval-check; rebase the seed)"
        )
    artifacts = TaskArtifacts(wt, bundle.eval_id)
    artifacts.copy_spec(spec_path)
    artifacts.append_log(
        action="developer",
        outcome="ok",
        round=1,
        role="developer",
        detail="implemented per spec",
    )
    sandbox.gitops.commit_all(
        wt, f"Round 1: developer changes for {bundle.eval_id}"
    )
    return wt


@dataclass
class EvalTools:
    """Injected gate collaborators (real impls in default_tools; fakes in
    tests). Mirrors run.Deps' wiring: the eval must measure the SAME
    instrument the VPS loop runs. Deliberate exception: quota handling is
    not wired (no QuotaPolicy) — a quota-starved run folds to
    inconclusive, the honest outcome for an interrupted measurement."""

    rw1_runner: AgentRunner
    rw2_runner: AgentRunner | None
    senior_runner: AgentRunner | None
    shell: ShellRunner
    docker_gate: DockerGate | None


def default_tools(config: OrchestratorConfig, repo: Path, *, docker: bool) -> EvalTools:
    from orchestrator.agents import (
        DEFAULT_CLAUDE_CMD,
        DEFAULT_CODEX_CMD,
        DEFAULT_SENIOR_CMD,
        ClaudeRunner,
        CodexRunner,
    )
    from orchestrator.testgate import (
        DockerGate,
        _subprocess_shell,
        _subprocess_shell_gate,
    )

    docker_gate = None
    if docker:
        # The frontend gate command is per-target (M1); load it from the target
        # under test. Only needed for the authoritative Docker gate.
        from orchestrator.target_policy import load_target_policy

        docker_gate = DockerGate(
            frontend_gate=load_target_policy(repo).frontend_gate,
            shell=_subprocess_shell_gate,
        )
    return EvalTools(
        rw1_runner=ClaudeRunner(config.claude_cmd or DEFAULT_CLAUDE_CMD),
        rw2_runner=CodexRunner(config.codex_cmd or DEFAULT_CODEX_CMD),
        senior_runner=ClaudeRunner(config.senior_cmd or DEFAULT_SENIOR_CMD),
        shell=_subprocess_shell,
        docker_gate=docker_gate,
    )


def run_eval(
    repo_root: Path,
    eval_id: str,
    *,
    work_root: Path,
    tools: EvalTools,
    fast_commands: str,
    base: str = "main",
    fix_ref: str | None = None,
    keep: bool = False,
    record: bool = True,
) -> EvalOutcome:
    """One seeded-eval run: sandbox -> plant -> production loop -> fold.

    max_loops=1 caps the loop after the seed's single review round: a
    blocked seed must terminate (CAP_REACHED), never be 'fixed' - the
    NeverRunner developer makes that structural. The outcome is folded
    from the gate outputs (decision-independent) and, with ``record``,
    appended as a seeded-eval event to the REAL repo's oracle run log
    (commit it; push stays with the Director).
    """
    bundle = load_bundle(repo_root, eval_id)
    sandbox = make_sandbox(repo_root, work_root, base=base)
    try:
        wt = plant_seed(sandbox, bundle, work_root)
        # The seed IS the recorded developer output: dev-side roles
        # (explorer, debugger) have nothing to run inside the eval - their
        # runner is the NeverRunner, so leaving them in the composition
        # would abort every bug/spike eval at INTERNAL_ERROR before any
        # gate. The eval measures the gate chain only.
        roles = tuple(
            r for r in bundle.spec.roles if r not in ("explorer", "debugger")
        )
        orchestrator = Orchestrator(
            gitops=sandbox.gitops,
            dev_runner=NeverRunner(),
            rw1_runner=tools.rw1_runner,
            rw2_runner=tools.rw2_runner if "rw2" in roles else None,
            senior_runner=tools.senior_runner if "rw2" in roles else None,
            docker_gate=tools.docker_gate,
            composition=roles,
            policy_enabled=True,
            notifier=NtfyNotifier(topic=None),  # evals never ping the phone
            fast_commands=fast_commands,
            shell=tools.shell,
            roles_dir=default_roles_dir(),
            max_loops=1,
        )
        terminal = orchestrator.run(eval_id)
        artifacts = TaskArtifacts(wt, eval_id)
        entries = artifacts.read_log()
        verdicts = {
            "rw1": artifacts.read_json(RW1_VERDICT),
            "rw2": artifacts.read_json(RW2_VERDICT),
            "senior": artifacts.read_json(SENIOR_VERDICT),
        }
        decision_raw = artifacts.read_json(MERGE_DECISION)
        decision = (
            decision_raw.get("decision") if isinstance(decision_raw, dict) else None
        )
        outcome = fold_outcome(
            eval_id=eval_id,
            class_slug=bundle.class_slug,
            expected_files=bundle.files,
            entries=entries,
            verdicts=verdicts,
            terminal=terminal,
            decision=decision,
        )
    finally:
        if not keep:
            cleanup_sandbox(work_root)
    if record:
        append_eval(
            repo_root,
            eval_id=eval_id,
            class_slug=bundle.class_slug,
            result=outcome.result,
            caught_by=outcome.caught_by,
            terminal=outcome.terminal,
            decision=outcome.decision,
            fix_ref=fix_ref,
        )
    return outcome
