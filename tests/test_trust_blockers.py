"""Regression tests for the trust-model / detached-loop blockers fixed in the
verify-before-swap review of the .laddy bundle. Each test pins a specific
failure mode so a future edit that reopens the hole fails loudly. Numbered to
the review findings.

Pure-function coverage; no Docker, no git, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME, agents, policy, testgate, verdict
from orchestrator.agents import AgentResult
from orchestrator.local_merge import (
    DRY_RUN,
    ArtifactAttestation,
    ArtifactAttestationState,
    GateResults,
    LocalMergeEngine,
    decide,
)
from orchestrator.loop import _recorded_terminal
from orchestrator.target_policy import TargetPolicy

_POL = TargetPolicy.myapp()  # classify tests exercise the myapp policy (M1)

# --------------------------------------------------------------------------
# #1 - the binding gate's pass/fail is the CONTAINER EXIT CODE, not the parsed
# @@GATE line: untrusted in-container code cannot forge the exit status, so a
# forged all-pass line on a non-zero exit must never clear the gate.
# --------------------------------------------------------------------------

_GREEN = "@@GATE lint=0 types=0 tests=0 coverage=0 semgrep=0 gitleaks=0"


def test_zero_container_exit_is_authoritative_green() -> None:
    r = testgate.parse_binding_output("pytest ran\n", container_rc=0)
    assert r.tests_passed and r.coverage_ok and not r.scan_findings


def test_forged_all_green_line_with_nonzero_exit_is_failed() -> None:
    # C1 (the leaked-nonce exploit): branch code reads GATE_COMMAND, learns the
    # marker, and prints a genuine all-pass @@GATE line. It still cannot alter
    # the trusted tail's `exit`, so the non-zero container exit holds the gate.
    r = testgate.parse_binding_output(f"{_GREEN}\n", container_rc=1)
    assert not r.tests_passed and not r.coverage_ok and r.scan_findings


def test_nonzero_exit_with_no_line_fails_closed() -> None:
    # container/build/timeout death before the diagnostic echo: fail closed.
    r = testgate.parse_binding_output("docker: build error\n", container_rc=1)
    assert not r.tests_passed and not r.coverage_ok and r.scan_findings


def test_the_gate_command_ends_with_a_composite_exit() -> None:
    # the exit status carries the verdict, so the gate must actually set it.
    cmd = testgate.BindingGate(compose_rel="c.yml").command("sha", "myapp")
    assert "exit $(( L || T || P || C || S || G ))" in cmd


# --------------------------------------------------------------------------
# #3 - docs/** must not admit executable code to L1 (fnmatch '*' crosses '/')
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["docs/x/evil.py", "docs/deploy.sh", "docs/a/b/c.yml"])
def test_docs_code_is_not_l1(path: str) -> None:
    assert policy.classify_blast_radius(_POL, [path]) == "L2"


@pytest.mark.parametrize("path", ["docs/foo.md", "README.md", "docs/a/b/note.md"])
def test_docs_markdown_stays_l1(path: str) -> None:
    assert policy.classify_blast_radius(_POL, [path]) == "L1"


# --------------------------------------------------------------------------
# #4 - a verdict is only trusted from a run that finished cleanly
# --------------------------------------------------------------------------

_APPROVED = json.dumps(
    {
        "verdict": "APPROVED",
        "risk_level": "low",
        "files_reviewed": [],
        "claims_verified": [],
        "findings": [],
        "test_assessment": "ok",
        "residual_risks": [],
    }
)


class _FakeRunner:
    name = "fake"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def run(self, prompt: str, cwd: Path, resume: str | None = None) -> AgentResult:
        rc = 0 if self._reason == "ok" else 1
        return AgentResult(text=_APPROVED, session_id=None, exit_reason=self._reason, returncode=rc)


def test_clean_run_verdict_trusted() -> None:
    v, _ = verdict.request_verdict(_FakeRunner("ok"), "p", Path("."), max_retries=0)
    assert v.approved


@pytest.mark.parametrize("reason", ["error", "quota"])
def test_non_ok_run_verdict_refused(reason: str) -> None:
    # a valid APPROVED payload carried by an errored/quota run must NOT clear the gate
    with pytest.raises(verdict.VerdictError):
        verdict.request_verdict(_FakeRunner(reason), "p", Path("."), max_retries=1)


# --------------------------------------------------------------------------
# #6 / #7 - a missing CLI (or exec failure) is a non-zero result, never a crash
# --------------------------------------------------------------------------


def test_missing_cli_maps_to_error_not_crash() -> None:
    rc, out, err = agents._subprocess_exec(["myapp-no-such-binary-zzz"], Path("."), "hi")
    assert rc != 0 and "exec failed" in err and out == ""


# --------------------------------------------------------------------------
# #8 - terminal states are replay-idempotent, but INTERNAL_ERROR is retryable
# --------------------------------------------------------------------------


def test_recorded_terminal_detects_terminal_entry() -> None:
    log = [
        {"action": "developer", "outcome": "ok"},
        {"action": "terminal", "outcome": "CAP_REACHED"},
    ]
    assert _recorded_terminal(log) == "CAP_REACHED"


def test_recorded_terminal_detects_completed_push() -> None:
    log = [{"action": "verify", "outcome": "ok"}, {"action": "push", "outcome": "ok"}]
    assert _recorded_terminal(log) == "PUSHED"


def test_recorded_terminal_internal_error_is_retryable() -> None:
    # an internal error records+notifies but must NOT be sticky: a re-kickoff
    # resumes from the last phase instead of returning the stale error forever.
    log = [
        {"action": "developer", "outcome": "ok"},
        {"action": "terminal", "outcome": "INTERNAL_ERROR"},
    ]
    assert _recorded_terminal(log) is None


def test_recorded_terminal_none_mid_run() -> None:
    log = [{"action": "developer", "outcome": "ok"}, {"action": "rw1", "outcome": "approved"}]
    assert _recorded_terminal(log) is None


# --------------------------------------------------------------------------
# #10 - --no-input is a true dry run: reports would-merge, mutates nothing
# --------------------------------------------------------------------------


def _green_l1() -> GateResults:
    return GateResults(
        blast="L1",
        artifact_attestation=ArtifactAttestation(ArtifactAttestationState.PASSED),
        tests_passed=True,
        tests_tail="",
        coverage_ok=True,
        coverage_detail="",
        scan_findings=(),
        rw2=None,
        security_verdicts=(),
        sensitive_files=(),
        head_sha="abc123def456",
    )


def test_green_l1_auto_merges_without_dry_run() -> None:
    assert decide("t1", _green_l1()).kind == "auto_merge"
    merged: list[tuple[str, str]] = []
    engine = LocalMergeEngine(
        list_ready=lambda: ["t1"],
        verify_one=lambda t: _green_l1(),
        merge_one=lambda request: (
            merged.append((request.task_id, request.verified_sha)) or True
        ),
        # H4: even a green L1 auto-merge is gated on the merge-safety
        # confirmation; this test confirms it to exercise the merge path.
        confirm=lambda v: True,
    )
    results = engine.run()
    assert merged == [("t1", "abc123def456")]
    assert results[0].merged


def test_dry_run_holds_green_l1_and_merges_nothing() -> None:
    merged: list[tuple[str, str]] = []
    engine = LocalMergeEngine(
        list_ready=lambda: ["t1"],
        verify_one=lambda t: _green_l1(),
        merge_one=lambda request: (
            merged.append((request.task_id, request.verified_sha)) or True
        ),
        dry_run=True,
    )
    results = engine.run()
    assert merged == []  # nothing touched local main
    assert results[0].kind == DRY_RUN and not results[0].merged


# --------------------------------------------------------------------------
# #12 - deploy/secret config is sensitive
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [".env", ".env.production", "firebase.json", ".firebaserc", "sub/.env.local"]
)
def test_deploy_secret_config_is_sensitive(path: str) -> None:
    assert policy.classify_blast_radius(_POL, [path]) == "L3"


# --------------------------------------------------------------------------
# C2 - branch-shipped agent config (hooks / MCP servers / steering files) can
# execute host commands or steer the local reviewer, so it is L3 (human-gated),
# never an L1/L2 auto-merge; the review worktree also strips it before any CLI.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks/run.sh",
        ".mcp.json",
        ".codex/hooks.json",
        "CLAUDE.md",
        "AGENTS.md",
        "GEMINI.md",
        "sub/CLAUDE.md",
    ],
)
def test_agent_config_surface_is_l3(path: str) -> None:
    assert policy.classify_blast_radius(_POL, [path]) == "L3"


# --------------------------------------------------------------------------
# C3 - a report-only task may PROPOSE fix specs only as drafts; it must not
# auto-merge a self-authored `status: ready` spec that would run on next kickoff.
# --------------------------------------------------------------------------


def _spec_reader(files: dict[str, str]):
    return lambda p: files[p]


def test_report_only_ready_spec_is_refused() -> None:
    evil = f"{TARGET_DIR_NAME}/specs/evil.md"
    fix = f"{TARGET_DIR_NAME}/specs/t1-fix.md"
    changed = [evil, fix]
    reader = _spec_reader(
        {
            evil: "---\nstatus: ready\n---\n# evil autonomous task\n",
            fix: "---\nstatus: draft-proposal\n---\n# proposed fix\n",
        }
    )
    nondraft = policy.nondraft_report_specs("t1", changed, reader)
    assert nondraft == [evil]  # only the ready spec, not the draft fix
    d = policy.report_only_decision(
        task_id="t1", changed_files=changed, verify_confirmed=True, nondraft_specs=nondraft
    )
    assert d.decision == "stop_before_merge"
    assert any("nondraft_spec" in r for r in d.reasons)


def test_report_only_missing_status_spec_is_refused() -> None:
    # a spec with NO status is not a draft -> runnable on merge -> refused.
    other = f"{TARGET_DIR_NAME}/specs/other.md"
    reader = _spec_reader({other: "# no front matter\n"})
    assert policy.nondraft_report_specs("t1", [other], reader) == [other]


def test_report_only_own_spec_may_be_nondraft() -> None:
    # the task's own (input) audit spec is runnable by design; only PROPOSED
    # specs must be drafts, so the own spec is exempt.
    own = f"{TARGET_DIR_NAME}/specs/t1.md"
    reader = _spec_reader({own: "---\ntype: audit\n---\n# t1 audit\n"})
    assert policy.nondraft_report_specs("t1", [own], reader) == []


def test_report_only_own_spec_flipped_executable_is_refused() -> None:
    # H3: the own-spec exemption is by FILENAME, so without a content check a
    # report-only task could rewrite specs/<task>.md into a runnable non-report
    # task (report_only -> off) and land a self-authored executable spec.
    own = f"{TARGET_DIR_NAME}/specs/t1.md"
    reader = _spec_reader(
        {own: "---\ntype: feature\nstatus: ready\n---\n# injected instructions\n"}
    )
    nondraft = policy.nondraft_report_specs("t1", [own], reader)
    assert nondraft == [own]
    d = policy.report_only_decision(
        task_id="t1", changed_files=[own], verify_confirmed=True, nondraft_specs=nondraft
    )
    assert d.decision == "stop_before_merge"
    assert any("nondraft_spec" in r for r in d.reasons)


def test_report_only_own_spec_promoted_to_ready_is_refused() -> None:
    # H3: still report-only in type, but promoted to a runnable status - the
    # merged spec would re-run autonomously on a later kickoff/enqueue.
    own = f"{TARGET_DIR_NAME}/specs/t1.md"
    reader = _spec_reader(
        {own: "---\ntype: audit\nstatus: ready\n---\n# injected instructions\n"}
    )
    assert policy.nondraft_report_specs("t1", [own], reader) == [own]


def test_report_only_own_spec_unparseable_is_refused() -> None:
    # an unparseable own spec cannot be certified still-report-only: fail closed
    own = f"{TARGET_DIR_NAME}/specs/t1.md"
    reader = _spec_reader({own: "---\ntype: audit\nno front matter close\n"})
    assert policy.nondraft_report_specs("t1", [own], reader) == [own]


def test_report_only_own_spec_may_stay_report_only_nondraft() -> None:
    # the legitimate own-spec edits (clarify appends a ## Clarifications block,
    # wording tweaks) keep type/status untouched and must stay exempt.
    own = f"{TARGET_DIR_NAME}/specs/t1.md"
    reader = _spec_reader(
        {own: "---\ntype: audit\n---\n# t1 audit\n\n## Clarifications\n- a: b\n"}
    )
    assert policy.nondraft_report_specs("t1", [own], reader) == []


def test_report_only_all_drafts_auto_merges() -> None:
    fix = f"{TARGET_DIR_NAME}/specs/t1-fix.md"
    reader = _spec_reader({fix: "---\nstatus: draft-proposal\n---\n# fix\n"})
    nondraft = policy.nondraft_report_specs("t1", [fix], reader)
    assert nondraft == []
    d = policy.report_only_decision(
        task_id="t1", changed_files=[fix], verify_confirmed=True, nondraft_specs=nondraft
    )
    assert d.decision == "auto_merge"


# --------------------------------------------------------------------------
# #13 - the verdict JSON extractor handles nested objects + braces in strings
# --------------------------------------------------------------------------


def test_extract_json_keeps_nested_objects() -> None:
    text = (
        "```json\n"
        '{"verdict":"APPROVED","findings":[{"severity":"blocker","line":1}],'
        '"meta":{"nested":{"deep":true}}}\n'
        "```"
    )
    obj = json.loads(verdict.extract_json(text))
    assert obj["meta"]["nested"]["deep"] is True
    assert obj["findings"][0]["severity"] == "blocker"


def test_extract_json_ignores_braces_inside_strings() -> None:
    text = 'prefix {"summary":"handle {curly} braces","n":7} suffix'
    assert json.loads(verdict.extract_json(text))["n"] == 7


def test_extract_json_full_verdict_with_findings_roundtrips() -> None:
    v = verdict.parse_verdict(
        "```json\n"
        '{"verdict":"CHANGES_REQUESTED","risk_level":"high","files_reviewed":["a.py"],'
        '"claims_verified":[{"claim":"x","evidence":"y","verified":false}],'
        '"findings":[{"severity":"blocker","category":"correctness","file":"a.py",'
        '"line":10,"summary":"boom","failure_scenario":"null deref on empty input"}],'
        '"test_assessment":"weak","residual_risks":["z"]}\n```'
    )
    assert not v.approved and len(v.blockers) == 1
