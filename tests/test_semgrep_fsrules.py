"""Tests for the deterministic FS-safety semgrep rules (fullrun-s2).

Two layers:

* Rule-behaviour tests (AC1/AC2/AC3) invoke the pinned semgrep on the real
  in-repo corpus and on anti-pattern fixtures. They SKIP where semgrep is
  absent (the Director's own box) and run inside the gate container, which
  carries semgrep==1.169.0 - mirroring the ``requires_bash`` idiom in
  ``test_testgate``. A green local run with these SKIPPED is therefore NOT full
  verification of rule behaviour; the gate exercises them.
* Invariant tests (AC4/AC5/AC6/AC7/AC8) are pure file / import assertions that
  run everywhere: two-copy byte identity, the offline+pinned config, the trust
  boundary, the not-encoded documentation, and the severity note.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RULESET = REPO / ".laddy" / "security" / "semgrep.yml"
MIRROR = REPO / "security" / "semgrep.yml"
FIXTURES = REPO / "tests" / "fixtures" / "semgrep"

QUEUE = REPO / "orchestrator" / "queue.py"
ARTIFACTS = REPO / "orchestrator" / "artifacts.py"
REPORT_PATH = REPO / "monitoring" / "loop_monitor" / "report_path.py"

RULE_A = "python-open-without-nofollow-or-excl"
RULE_B = "python-ftruncate-without-nlink-check"
RULE_B2 = "python-open-trunc-without-nlink-check"

requires_semgrep = pytest.mark.skipif(
    shutil.which("semgrep") is None,
    reason="semgrep runs in the gate container (semgrep==1.169.0); the Director's box lacks it",
)


def _scan(*targets: Path) -> set[tuple[str, str, int]]:
    """Run the committed ruleset over ``targets``; return {(rule_id, filename, line)}.

    Explicit targets override semgrep's default ``tests/`` ignore, so this can
    scan the fixtures directly even though the gate's ``semgrep ... .`` skips
    them. ``check_id`` is emitted with a path prefix (e.g. ``semgrep.<id>``);
    rule ids carry no dots, so the last dot-segment is the bare id.
    """
    proc = subprocess.run(
        ["semgrep", "--json", "--config", str(RULESET), *(str(t) for t in targets)],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    data = json.loads(proc.stdout)
    assert data.get("errors") == [], f"semgrep reported errors: {data.get('errors')}"
    return {
        (r["check_id"].rsplit(".", 1)[-1], Path(r["path"]).name, r["start"]["line"])
        for r in data["results"]
    }


def _lines(found: set[tuple[str, str, int]], rule_id: str, filename: str) -> set[int]:
    return {line for rid, name, line in found if rid == rule_id and name == filename}


# --- AC1: each shipped rule catches its anti-pattern -------------------------


@requires_semgrep
def test_rule_a_fires_on_unguarded_write_open_fixture() -> None:
    found = _scan(FIXTURES / "rule_a_bad_write_open.py")
    assert _lines(found, RULE_A, "rule_a_bad_write_open.py")


@requires_semgrep
def test_rule_b_fires_on_ftruncate_without_nlink_fixture() -> None:
    found = _scan(FIXTURES / "rule_b_bad_ftruncate.py")
    assert _lines(found, RULE_B, "rule_b_bad_ftruncate.py")


@requires_semgrep
def test_rule_b2_fires_on_open_trunc_without_nlink_fixture() -> None:
    found = _scan(FIXTURES / "rule_b2_bad_open_trunc.py")
    assert _lines(found, RULE_B2, "rule_b2_bad_open_trunc.py")


# --- AC2: zero false positives on the in-repo corpus (outranks AC1) ----------


@requires_semgrep
def test_rule_a_no_false_positive_on_guarded_corpus() -> None:
    found = _scan(REPORT_PATH, QUEUE)
    # report_path.py: every os.open there carries O_NOFOLLOW (or is O_RDONLY).
    assert _lines(found, RULE_A, "report_path.py") == set()
    q = _lines(found, RULE_A, "queue.py")
    # queue.py:104 is THE canary - O_CREAT|O_EXCL is TOCTOU-free without
    # O_NOFOLLOW; 156/177 are O_RDONLY / O_NOFOLLOW opens.
    assert 104 not in q, "Rule A fired on the O_EXCL canary queue.py:104"
    assert 156 not in q and 177 not in q


@requires_semgrep
def test_rule_b_no_false_positive_on_guarded_report_path() -> None:
    # report_path.py:129 truncates, but its enclosing function checks
    # st_nlink first (via the deep-expression operator, which matches the
    # `if info.st_nlink != 1:` condition the naive `... st_nlink ...` misses).
    found = _scan(REPORT_PATH)
    assert _lines(found, RULE_B, "report_path.py") == set()


@requires_semgrep
def test_rule_b2_no_false_positive_on_report_path_or_guarded_corpus() -> None:
    # report_path.py has no O_TRUNC-on-open site at all; queue.py's only
    # O_TRUNC site (177) is the adjudicated finding (AC3), not a false
    # positive - covered separately below.
    found = _scan(REPORT_PATH)
    assert _lines(found, RULE_B2, "report_path.py") == set()


# --- AC3: the two unguarded sites are adjudicated as genuine findings --------


@requires_semgrep
def test_rule_a_reports_the_two_unguarded_sites_as_findings() -> None:
    # artifacts.py:143 and queue.py:205 open O_CREAT|O_RDWR with neither guard.
    # Adjudicated (ruleset header) as genuine low-severity findings: the
    # "engine-derived path" defence is a provenance argument the ruleset refuses
    # to encode, and queue.py:205 lacks the O_NOFOLLOW its siblings carry. The
    # rule correctly fires; --baseline-commit keeps main green as they predate
    # any diff. This locks the adjudication in as a finding, not a suppression.
    found = _scan(ARTIFACTS, QUEUE)
    assert 143 in _lines(found, RULE_A, "artifacts.py")
    assert 205 in _lines(found, RULE_A, "queue.py")


@requires_semgrep
def test_rule_b2_reports_queue_py_lock_pid_write_as_a_finding() -> None:
    # queue.py:177 (_write_lock_pid) carries O_NOFOLLOW (Rule A silent) but
    # opens O_CREAT|O_WRONLY|O_TRUNC on a pre-existing path with no st_nlink
    # check - adjudicated (ruleset header) alongside the Rule A sites above,
    # same provenance argument, same --baseline-commit treatment.
    found = _scan(QUEUE)
    assert 177 in _lines(found, RULE_B2, "queue.py")


# --- AC5: the two copies stay byte-identical ---------------------------------


def test_two_ruleset_copies_are_byte_identical() -> None:
    assert RULESET.read_bytes() == MIRROR.read_bytes()


# --- AC6: offline and pinned -------------------------------------------------


def test_gate_semgrep_is_offline_and_pinned() -> None:
    from orchestrator.testgate import SEMGREP_CONFIG, _binding_gate

    assert SEMGREP_CONFIG == ".laddy/security/semgrep.yml"
    cmd = _binding_gate("myapp")
    assert f"semgrep --error --config {SEMGREP_CONFIG} --baseline-commit" in cmd
    assert "--config auto" not in cmd


def test_ruleset_pulls_nothing_over_the_network() -> None:
    # The ruleset is a self-contained local `rules:` document: no registry pack
    # reference, no URL, no `--config auto`. The header PROSE names the
    # registry/`--config auto` as the thing AVOIDED, so assert on the actual
    # rule body (comment lines stripped), not the whole file.
    text = RULESET.read_text()
    assert re.search(r"(?m)^rules:$", text), "not a rules: document"
    body = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "--config auto" not in body
    assert "http://" not in body and "https://" not in body
    # every rule is a local pattern rule (no external include pulls the ruleset
    # off-box): each `- id:` block declares patterns/pattern-either/pattern.
    blocks = re.split(r"(?m)^  - id: ", body)[1:]
    assert blocks, "no rules parsed"
    for blk in blocks:
        assert re.search(r"\n    (patterns|pattern-either|pattern):", blk), blk


# --- AC7: trust boundary untouched -------------------------------------------


def test_trusted_ref_passed_only_by_local_merge() -> None:
    # Only the local end supplies trusted_ref (it restores .laddy/security from
    # trusted main before scanning); the VPS DockerGate never does. Exactly one
    # caller passes it as a keyword argument.
    callers = [
        f"{py.name}:{i}"
        for py in sorted((REPO / "orchestrator").glob("*.py"))
        for i, line in enumerate(py.read_text().splitlines(), 1)
        if "trusted_ref=" in line
    ]
    assert len(callers) == 1, f"expected exactly one trusted_ref= caller, got {callers}"
    assert callers[0].startswith("local_merge.py:")


def test_no_vps_scan_result_is_authoritative_gate_semantics_unchanged() -> None:
    # The VPS pre-filter builds no trusted-ref infra restore; authority is the
    # container exit code composed over every gate (semgrep included), which
    # untrusted in-container code cannot forge.
    from orchestrator.testgate import BindingGate, _binding_gate

    vps_cmd = BindingGate(compose_rel="c.yml").command("branchsha", "myapp")
    assert ".laddy/docker .laddy/security" not in vps_cmd  # no restore on VPS
    gate = _binding_gate("myapp")
    assert gate.rstrip().endswith("exit $(( L || T || P || C || S || G ))")


# --- AC4 + AC8: documentation of not-encoded classes and severity ------------


def _rule_severity(text: str, rule_id: str) -> str:
    m = re.search(
        rf"- id: {re.escape(rule_id)}\b(.*?)(?=\n  - id:|\Z)", text, re.S
    )
    assert m, f"rule {rule_id} not found in ruleset"
    sev = re.search(r"severity:\s*(\w+)", m.group(1))
    assert sev, f"no severity for {rule_id}"
    return sev.group(1)


def test_not_encoded_classes_are_documented() -> None:
    text = RULESET.read_text()
    low = text.lower()
    assert "not encoded" in low, "header must record the not-encoded classes"
    assert "rule c" in low and "rule d" in low
    # Rule C's reason names report_path/realpath; Rule D's names taint/provenance.
    assert "realpath" in low
    assert "taint" in low or "provenance" in low


def test_severity_behaviour_recorded_and_new_rules_are_error() -> None:
    text = RULESET.read_text()
    low = text.lower()
    # AC8: header records that --error blocks on WARNING too, from an actual run.
    assert "--error" in text and "warning" in low
    # each new rule's severity is a deliberate ERROR (they flag real defects).
    assert _rule_severity(text, RULE_A) == "ERROR"
    assert _rule_severity(text, RULE_B) == "ERROR"
    assert _rule_severity(text, RULE_B2) == "ERROR"
