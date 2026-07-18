"""Tests for merge policy path rules, risk tiers and the merge decision."""

from __future__ import annotations

from functools import partial

from orchestrator import TARGET_DIR_NAME
from orchestrator import policy as _policy
from orchestrator.policy import (
    GateStates,
    effective_risk,
    path_guard,
    report_only_decision,
)
from orchestrator.target_policy import TargetPolicy

# These tests exercise the myapp policy; bind it once so the per-target `policy`
# argument (M1) does not need repeating at all ~90 call sites. Per-target LOADING
# is covered by test_target_policy.py.
_POL = TargetPolicy.myapp()
sensitive_paths = partial(_policy.sensitive_paths, _POL)
security_paths = partial(_policy.security_paths, _POL)
computed_risk = partial(_policy.computed_risk, _POL)
touches_invariant_tests = partial(_policy.touches_invariant_tests, _POL)
user_visible = partial(_policy.user_visible, _POL)
classify_blast_radius = partial(_policy.classify_blast_radius, _POL)
spec_is_high_risk = partial(_policy.spec_is_high_risk, _POL)
merge_decision = partial(_policy.merge_decision, policy=_POL)


def _gates(
    sha: str = "abc",
    rw1: bool = True,
    rw2: bool = True,
    auth: bool = True,
    flaky: bool = False,
    rw1_sha: str | None = None,
    rw2_sha: str | None = None,
    auth_sha: str | None = None,
) -> GateStates:
    return GateStates(
        head_sha=sha,
        rw1_sha=rw1_sha or sha,
        rw1_approved=rw1,
        rw2_sha=rw2_sha or sha,
        rw2_go=rw2,
        authoritative_sha=auth_sha or sha,
        authoritative_passed=auth,
        authoritative_flaky=flaky,
    )


def test_path_guard_allows_task_artifacts_and_markdown_specs() -> None:
    ok, offending = path_guard(
        "t1",
        [
            f"{TARGET_DIR_NAME}/tasks/t1/report.md",
            f"{TARGET_DIR_NAME}/tasks/t1/findings.json",
            f"{TARGET_DIR_NAME}/specs/t1-fix.md",
        ],
    )
    assert ok is True
    assert offending == []


def test_path_guard_blocks_docs_tree_and_non_md_spec_files() -> None:
    # LOW (C2+C3 audit): docs/ admitted executable files (docs/conftest.py runs
    # at every host pytest after merge; there is no docs/ tree by design), and
    # a non-.md file under the spec dir is not an inert proposal. Both out.
    for path in (
        "docs/conftest.py",
        "docs/development/notes.md",
        f"{TARGET_DIR_NAME}/specs/evil.py",
        f"{TARGET_DIR_NAME}/specs/conftest.py",
    ):
        ok, offending = path_guard("t1", [path])
        assert ok is False, path
        assert offending == [path], path


def test_path_guard_blocks_source_files() -> None:
    ok, offending = path_guard("t1", [f"{TARGET_DIR_NAME}/tasks/t1/report.md", "myapp/models.py"])
    assert ok is False
    assert offending == ["myapp/models.py"]


def test_path_guard_blocks_other_tasks_artifacts() -> None:
    ok, offending = path_guard("t1", [f"{TARGET_DIR_NAME}/tasks/OTHER/report.md"])
    assert ok is False
    assert offending == [f"{TARGET_DIR_NAME}/tasks/OTHER/report.md"]


def test_path_guard_blocks_tests_dir() -> None:
    # investigator must not commit failing tests (design S3)
    ok, offending = path_guard("t1", ["tests/test_new_repro.py"])
    assert ok is False


def test_touches_invariant_tests() -> None:
    assert touches_invariant_tests(["tests/test_architecture_contracts.py"])
    assert touches_invariant_tests(["tests/test_inbox_audit_append_only.py"])
    assert not touches_invariant_tests(["tests/test_api_games.py", "myapp/models.py"])


# --- sensitive paths (S8 list; Appendix D point 5 exemptions) -----------------


def test_sensitive_globs_cover_the_s8_list() -> None:
    hits = sensitive_paths(
        [
            "myapp/models.py",
            "myapp/infrastructure/runtime_db.py",
            "alembic/versions/123_add_col.py",
            "tests/test_architecture_contracts.py",
            "tests/test_append_only_session_history.py",
            "cloudbuild.yaml",
            "scripts/release-prod.ps1",
            "docker-entrypoint.sh",
            # engine surfaces at the REPO ROOT (repo_laddy: post-split, the
            # engine's own code lives here, never under TARGET_DIR_NAME)
            "roles/developer.md",
            "orchestrator/loop.py",
            "scripts/kickoff.sh",
            ".github/workflows/agent-merge.yml",
        ]
    )
    assert len(hits) == 12


def test_task_artifacts_and_specs_are_exempt() -> None:
    # the round-2 bugfix: blanket <agent-dir>/** would trip on every PR
    assert sensitive_paths(
        [f"{TARGET_DIR_NAME}/tasks/t1/iteration-log.jsonl", f"{TARGET_DIR_NAME}/specs/t1.md"]
    ) == []


def test_ordinary_source_not_sensitive() -> None:
    assert sensitive_paths(["myapp/api/routers/games.py", "frontend/src/App.tsx"]) == []


def test_dependency_and_supply_chain_files_are_sensitive() -> None:
    # trust-model doc S9: a dependency bump is L3 (supply-chain is the main
    # realistic attack vector), so manifests/lockfiles/image build must be
    # sensitive - previously they slipped through to L2 auto-merge.
    for path in (
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "package.json",
        "frontend/package.json",
        "pnpm-lock.yaml",
        "frontend/pnpm-lock.yaml",
        "Dockerfile",
    ):
        assert sensitive_paths([path]) == [path], path


def test_payments_and_ingress_surfaces_are_security() -> None:
    # design S8 names "payments" as sensitive, but no glob covered it; likewise
    # session management (authn-adjacent) and the external webhook ingress.
    for path in (
        "myapp/api/routers/payments.py",
        "myapp/application/payments/purchase_order_service.py",
        "myapp/infrastructure/payments/fio_client.py",
        "myapp/api/routers/sessions.py",
        "myapp/api/routers/telegram_webhook.py",
    ):
        assert security_paths([path]) == [path], path


def test_dependency_and_payment_files_classify_l3() -> None:
    from orchestrator.policy import L3

    assert classify_blast_radius(["requirements.txt"]) == L3
    assert classify_blast_radius(["myapp/api/routers/payments.py"]) == L3
    assert classify_blast_radius(["frontend/pnpm-lock.yaml"]) == L3


def test_gate_own_security_ruleset_is_sensitive() -> None:
    # NÁLEZ 4: the gate's own semgrep ruleset (<agent-dir>/security/*) must be
    # sensitive - a branch that weakens the rules must not auto-merge as L2,
    # or the weakened rules become the trusted version next time.
    from orchestrator.policy import L3

    semgrep_path = f"{TARGET_DIR_NAME}/security/semgrep.yml"
    assert sensitive_paths([semgrep_path]) == [semgrep_path]
    assert classify_blast_radius([semgrep_path]) == L3


def test_prompts_are_sensitive() -> None:
    # Oracle-design rw 2026-07-12: prompts/* hold the external audit/oracle
    # prompts - the measurement instrument for the gates. Post-split (spec
    # 2026-07-13 S3 step 0) these are an ENGINE resource read from
    # ENGINE_DIR/prompts, i.e. the REPO ROOT when laddy itself is the task
    # branch's target - not TARGET_DIR_NAME/prompts (dead post-split; no
    # target repo's diff can contain that path anymore). As .md they'd
    # otherwise classify safe-by-construction (L1), letting a branch rewrite
    # the instrument through the no-review auto-merge path.
    from orchestrator.policy import L3

    prompt_path = "prompts/self-improvement-audit.md"
    assert sensitive_paths([prompt_path]) == [prompt_path]
    assert classify_blast_radius([prompt_path]) == L3


def test_oracle_data_files_are_sensitive() -> None:
    # Oracle design 2026-07-12, re-keyed post-split (spec 2026-07-13 S3 step
    # 0): the append-only run log (the escape-rate time series) is genuinely
    # TARGET-side state (orchestrator/oracle/runlog.py RUN_LOG_PATH), so it
    # stays TARGET_DIR_NAME-prefixed. The escape-class registry moved to the
    # ENGINE install (ENGINE_DIR/oracle/classes.md) - at the REPO ROOT when
    # laddy itself is the target - so it's covered by the root-level
    # "oracle/*" glob instead. Both would otherwise classify
    # safe-by-construction (L1: .md/.jsonl) and let a branch rewrite the
    # measurement instrument through the no-review auto-merge path.
    assert sensitive_paths([f"{TARGET_DIR_NAME}/oracle/run-log.jsonl"]) == [
        f"{TARGET_DIR_NAME}/oracle/run-log.jsonl"
    ]
    assert sensitive_paths(["oracle/classes.md"]) == ["oracle/classes.md"]


# --- risk ---------------------------------------------------------------------


def test_computed_risk_tiers() -> None:
    assert computed_risk(["myapp/models.py"], 10) == "high"
    assert computed_risk(["a.py"] * 20, 10) == "medium"
    assert computed_risk(["a.py"], 500) == "medium"
    assert computed_risk(["a.py"], 50) == "low"


def test_effective_risk_is_max() -> None:
    assert effective_risk("high", "low") == "high"
    assert effective_risk("low", "medium") == "medium"
    assert effective_risk("low", "low") == "low"


def test_effective_risk_normalizes_out_of_enum_declared_to_high() -> None:
    # M8: an out-of-enum declared risk was ORDERED as high (rank 2) but
    # returned as the RAW string, and the only consumer compares == "high" -
    # fail-safe inverted. It must come back as the enum value "high".
    assert effective_risk("HIGH", "low") == "high"
    assert effective_risk("critical", "low") == "high"
    assert effective_risk("unknown-nonsense", "medium") == "high"
    # an absent declaration is the caller's legitimate default, not junk
    assert effective_risk("", "low") == "low"


def test_merge_decision_stops_on_out_of_enum_declared_risk() -> None:
    # M8 acceptance: declared "HIGH" / "critical" produce the high_risk stop.
    for declared in ("HIGH", "critical"):
        d = merge_decision(
            changed_files=["a.py"],
            diff_lines=5,
            declared_risk=declared,
            gates=_gates(),
        )
        assert d.decision == "stop_before_merge", declared
        assert "high_risk" in d.reasons
        assert d.risk_level == "high"


def test_user_visible() -> None:
    assert user_visible(["frontend/src/App.tsx"]) is True
    assert user_visible(["apps/public/src/index.astro"]) is True
    assert user_visible(["myapp/models.py"]) is False


# --- merge decision matrix (S8) ------------------------------------------------


def test_auto_merge_low_risk_all_green() -> None:
    d = merge_decision(
        changed_files=["myapp/api/routers/games.py", "tests/test_api_games.py"],
        diff_lines=80,
        declared_risk="low",
        gates=_gates(),
    )
    assert d.decision == "auto_merge"
    assert d.reasons == ()


def test_auto_merge_notify_medium_user_visible() -> None:
    d = merge_decision(
        changed_files=["frontend/src/App.tsx"] * 20,
        diff_lines=100,
        declared_risk="low",
        gates=_gates(),
    )
    assert d.decision == "auto_merge_notify"
    assert d.risk_level == "medium"


def test_stop_on_sensitive_path() -> None:
    d = merge_decision(
        changed_files=["myapp/models.py"],
        diff_lines=5,
        declared_risk="low",
        gates=_gates(),
    )
    assert d.decision == "stop_before_merge"
    assert any("policy_sensitive_paths" in r for r in d.reasons)


def test_stop_on_stale_approval_sha() -> None:
    d = merge_decision(
        changed_files=["a.py"],
        diff_lines=5,
        declared_risk="low",
        gates=_gates(rw1_sha="OLD"),
    )
    assert d.decision == "stop_before_merge"
    assert "stale_or_missing_rw1_approval" in d.reasons


def test_stop_on_flaky_authoritative() -> None:
    d = merge_decision(
        changed_files=["a.py"],
        diff_lines=5,
        declared_risk="low",
        gates=_gates(flaky=True),
    )
    assert d.decision == "stop_before_merge"
    assert "flaky_authoritative_tests" in d.reasons


def test_stop_on_deleted_test_files() -> None:
    d = merge_decision(
        changed_files=["a.py"],
        diff_lines=5,
        declared_risk="low",
        gates=_gates(),
        changed_statuses={"tests/test_x.py": "D", "a.py": "M"},
    )
    assert d.decision == "stop_before_merge"
    assert any("test_files_deleted" in r for r in d.reasons)


def test_stop_on_deleted_test_under_configured_test_dir() -> None:
    # M4: the myapp sample policy declares src/tests/ and frontend/__tests__/
    # as extra test locations (test_dirs); a deletion there must raise
    # test_files_deleted exactly like a deletion under literal tests/.
    for deleted in ("src/tests/test_x.py", "frontend/__tests__/App.test.tsx"):
        d = merge_decision(
            changed_files=["a.py"],
            diff_lines=5,
            declared_risk="low",
            gates=_gates(),
            changed_statuses={deleted: "D", "a.py": "M"},
        )
        assert d.decision == "stop_before_merge", deleted
        assert any("test_files_deleted" in r for r in d.reasons), deleted


def test_deleted_tests_dir_detected_even_when_target_configures_nothing() -> None:
    # Fail-closed: literal tests/ is an ENGINE default a target can only ADD
    # to, never remove - a policy with empty test_dirs still detects it.
    from dataclasses import replace

    d = _policy.merge_decision(
        policy=replace(_POL, test_dirs=()),
        changed_files=["a.py"],
        diff_lines=5,
        declared_risk="low",
        gates=_gates(),
        changed_statuses={"tests/test_x.py": "D"},
    )
    assert d.decision == "stop_before_merge"
    assert any("test_files_deleted" in r for r in d.reasons)


def test_stop_on_destructive_migration() -> None:
    texts = {"alembic/versions/9_drop.py": "op.drop_table('usage_record')"}
    d = merge_decision(
        changed_files=["alembic/versions/9_drop.py"],
        diff_lines=5,
        declared_risk="low",
        gates=_gates(),
        migration_texts=lambda f: texts[f],
    )
    assert d.decision == "stop_before_merge"
    assert any("destructive_migrations" in r for r in d.reasons)


def test_stop_on_security_paths_and_senior_deadlock_and_unclear_intent() -> None:
    d = merge_decision(
        changed_files=["myapp/api/routers/auth_magic.py"],
        diff_lines=5,
        declared_risk="low",
        gates=_gates(),
        senior_deadlock=True,
        unclear_intent=True,
    )
    assert d.decision == "stop_before_merge"
    assert any("security_auth_paths" in r for r in d.reasons)
    assert "senior_escalation_without_clean_verdict" in d.reasons
    assert "unclear_product_intent" in d.reasons


def test_stop_on_declared_high_risk() -> None:
    d = merge_decision(
        changed_files=["a.py"], diff_lines=5, declared_risk="high", gates=_gates()
    )
    assert d.decision == "stop_before_merge"
    assert "high_risk" in d.reasons


# --- report-only decision -------------------------------------------------------


def test_report_only_auto_merge_on_guard_pass() -> None:
    d = report_only_decision(
        task_id="t1",
        changed_files=[f"{TARGET_DIR_NAME}/tasks/t1/report.md", f"{TARGET_DIR_NAME}/specs/t1-fix.md"],
        verify_confirmed=True,
    )
    assert d.decision == "auto_merge"


def test_report_only_stop_on_source_diff_or_missing_verify() -> None:
    d = report_only_decision(
        task_id="t1", changed_files=["myapp/models.py"], verify_confirmed=False
    )
    assert d.decision == "stop_before_merge"
    assert len(d.reasons) == 2


def test_spec_is_high_risk_by_front_matter():
    assert spec_is_high_risk("# t\nchange play SPA copy\n", "high") is True


def test_spec_is_high_risk_by_out_of_enum_declared_risk():
    # M8: an unknown declared level fails SAFE to high - "critical" must not
    # slip past a literal == "high" comparison.
    assert spec_is_high_risk("# t\nchange play SPA copy\n", "HIGH") is True
    assert spec_is_high_risk("# t\nchange play SPA copy\n", "critical") is True
    # absence stays a non-declaration, not junk
    assert spec_is_high_risk("# t\nchange play SPA copy\n", None) is False


def test_spec_is_high_risk_by_sensitive_path_in_text():
    # orchestrator/run.py is a root-level engine surface post-split (spec
    # 2026-07-13 S3 step 0), not TARGET_DIR_NAME/orchestrator/run.py (dead).
    body = "Goal: add a phase to `orchestrator/run.py`.\n"
    assert spec_is_high_risk(body, None) is True


def test_spec_is_high_risk_false_for_benign_spec():
    body = "Goal: tweak myapp/api/routers/games.py validation and add a test.\n"
    assert spec_is_high_risk(body, None) is False


def test_spec_is_high_risk_by_bare_sensitive_path():
    assert spec_is_high_risk(
        "Update orchestrator/run.py to add a phase.\n", None
    ) is True


def test_spec_is_high_risk_by_slashless_sensitive_filename():
    # LOW: a bare sensitive filename (no "/") must still hit the sensitive
    # globs - naming pyproject.toml / .env / CLAUDE.md is the same surface
    # whether or not the spec spells a directory prefix.
    assert spec_is_high_risk("Bump the pinned deps in pyproject.toml\n", None) is True
    assert spec_is_high_risk("Load settings from `.env` at startup.\n", None) is True
    assert spec_is_high_risk("Refresh the CLAUDE.md agent rules.\n", None) is True


def test_spec_is_high_risk_ignores_dotted_prose_tokens():
    # calibration: dotted prose (abbreviations, versions, bare filenames that
    # match no sensitive glob) must not explode into false positives.
    body = "Target python 3.11, e.g. tweak games.py validation, etc.\n"
    assert spec_is_high_risk(body, None) is False


# --- blast-radius classification (trust-model doc S8) -------------------------


def test_blast_radius_l1_safe_by_construction() -> None:
    from orchestrator.policy import L1, L3

    assert classify_blast_radius(["docs/x.md", "README.md"]) == L1
    assert classify_blast_radius(
        ["frontend/src/i18n/locales/cs/creator.json"]
    ) == L1
    # docs/** is NOT a blanket L1: fnmatch '*' crosses '/', so executable code
    # under docs/ must fall through to L2, never auto-merge unreviewed.
    assert classify_blast_radius(["docs/tools/gen.py"]) == "L2"
    # an empty changed set is an anomaly (a failed diff-gather), not "safe" -
    # it holds for a human (L3), it must never fail open into L1 auto-merge.
    assert classify_blast_radius([]) == L3


def test_spec_files_are_never_l1_even_as_pure_markdown() -> None:
    # H2: a spec is an executable task description, not inert markdown - a
    # merged `status: ready` spec runs autonomously on the next kickoff. An
    # all-markdown diff adding a spec must never ride the L1 no-review lane;
    # it falls to L2 so the security panel + rw2 gate it.
    from dataclasses import replace

    from orchestrator.policy import L2

    spec = f"{TARGET_DIR_NAME}/specs/next.md"
    assert classify_blast_radius([spec]) == L2
    # even mixed with genuinely inert markdown, the spec pulls the diff to L2
    assert classify_blast_radius(["README.md", spec]) == L2
    # engine guard: a target's safe_globs cannot re-admit the spec dir to L1
    pol = replace(_POL, safe_globs=(f"{TARGET_DIR_NAME}/specs/*.md",))
    assert _policy.classify_blast_radius(pol, [spec]) == L2


def test_added_test_files_are_not_safe_by_construction() -> None:
    from orchestrator.policy import L2

    # NÁLEZ 3: a test file is executable Python, not data. An added conftest.py
    # can neutralize the gate via collection hooks, and every merged test then
    # runs on the host at each `pytest -n auto`. So added tests are L2 (the
    # agents review them cheaply), never L1 safe-by-construction.
    assert classify_blast_radius(["tests/test_new.py"], {"tests/test_new.py": "A"}) == L2
    assert classify_blast_radius(["tests/conftest.py"], {"tests/conftest.py": "A"}) == L2


def test_blast_radius_l2_ordinary_logic() -> None:
    from orchestrator.policy import L2

    assert classify_blast_radius(["myapp/api/routers/games.py"]) == L2
    # a MODIFIED existing test is not safe-by-construction (could weaken it)
    assert classify_blast_radius(
        ["tests/test_existing.py"], {"tests/test_existing.py": "M"}
    ) == L2
    # docs + one logic file -> the logic pulls the whole diff to L2
    assert classify_blast_radius(
        ["docs/x.md", "myapp/api/routers/games.py"]
    ) == L2


def test_blast_radius_l3_sensitive_wins() -> None:
    from orchestrator.policy import L3

    assert classify_blast_radius(["myapp/models.py"]) == L3
    assert classify_blast_radius(["myapp/api/routers/auth_magic.py"]) == L3
    assert classify_blast_radius(["alembic/versions/9_x.py"]) == L3
    # even mixed with safe files, sensitive wins
    assert classify_blast_radius(["docs/x.md", "myapp/models.py"]) == L3
    # deleted invariant test (via --no-renames rename-away) is sensitive
    assert classify_blast_radius(
        ["tests/test_architecture_contracts.py"],
        {"tests/test_architecture_contracts.py": "D"},
    ) == L3


# --- post-split re-key: root engine surfaces + dead TARGET_DIR_NAME paths ----
# (spec 2026-07-13 S3 step 0: engine code lives at the ENGINE repo root; a
# target repo carries only TARGET_DIR_NAME/{specs,tasks,docker,security}.)


def test_root_engine_surface_classifies_l3() -> None:
    # repo_laddy: when laddy itself is the task branch's target, its engine
    # code lives at the repo root, not under TARGET_DIR_NAME.
    from orchestrator.policy import L3

    assert classify_blast_radius(["orchestrator/loop.py"]) == L3


def test_target_gate_infra_classifies_l3() -> None:
    # Gate infra IN THE TARGET (docker compose + semgrep ruleset) stays
    # sensitive for every target, regardless of which repo is the target.
    from orchestrator.policy import L3

    assert classify_blast_radius([f"{TARGET_DIR_NAME}/docker/compose.test.yml"]) == L3


def test_target_task_and_spec_artifacts_are_not_l3_by_these_globs() -> None:
    # Every task commits its own artifacts under specs/tasks - a blanket
    # TARGET_DIR_NAME/** rule would trip the L3 stop-list on every PR.
    from orchestrator.policy import L3

    assert classify_blast_radius([f"{TARGET_DIR_NAME}/specs/foo.md"]) != L3
    assert classify_blast_radius([f"{TARGET_DIR_NAME}/tasks/x/log.jsonl"]) != L3


def test_dead_old_style_target_engine_path_does_not_match() -> None:
    # Pre-split layout: engine code embedded inside the target under
    # TARGET_DIR_NAME/orchestrator/*. Post-split this path can never occur in
    # a real diff (the engine is a separate repo/install), so per "no dead
    # config" the old TARGET_DIR_NAME-prefixed glob was removed rather than
    # kept as a pattern that can never match. Confirm it stays unmatched
    # even if such a path showed up anyway (a weird/misconfigured target).
    assert sensitive_paths([f"{TARGET_DIR_NAME}/orchestrator/x.py"]) == []
    assert sensitive_paths([f"{TARGET_DIR_NAME}/roles/x.md"]) == []
    assert sensitive_paths([f"{TARGET_DIR_NAME}/scripts/x.sh"]) == []
    assert sensitive_paths([f"{TARGET_DIR_NAME}/prompts/x.md"]) == []


def test_oracle_eval_bundles_are_sensitive() -> None:
    # The eval bundles define the gates' validation instrument (apex risk):
    # they must ride the <agent-dir>/oracle/* sensitivity (fnmatch * crosses
    # "/"), so a task branch can never rewrite a seed through L1/L2.
    from orchestrator import TARGET_DIR_NAME

    path = f"{TARGET_DIR_NAME}/oracle/evals/e1/seed.patch"
    assert sensitive_paths([path]) == [path]
