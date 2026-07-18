"""Tests for per-target policy loading (M1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import TARGET_DIR_NAME
from orchestrator.target_policy import (
    ENGINE_SAFE_GLOBS,
    ENGINE_SENSITIVE_GLOBS,
    INERT_SAFE_EXTENSIONS,
    POLICY_REL,
    TargetPolicy,
    TargetPolicyError,
    dump_target_policy,
    load_target_policy,
    parse_target_policy,
)

_MINIMAL = """
coverage_package = "acme"
sensitive_globs = ["acme/models.py"]
security_globs = ["acme/auth.py"]
invariant_tests = ["tests/test_contract.py"]
test_dirs = ["src/tests/"]
migration_globs = ["migrations/*"]
frontend_prefixes = ["web/"]
frontend_gate = "npm run build"
user_visible_prefixes = ["web/"]
safe_globs = ["web/i18n/**/*.json"]
"""


def test_parse_minimal_policy() -> None:
    pol = parse_target_policy(_MINIMAL)
    assert pol.coverage_package == "acme"
    assert pol.sensitive_globs == ("acme/models.py",)
    assert pol.frontend_gate == "npm run build"


def test_all_sensitive_merges_engine_product_and_invariants() -> None:
    pol = parse_target_policy(_MINIMAL)
    merged = pol.all_sensitive_globs
    assert "acme/models.py" in merged  # product
    assert "tests/test_contract.py" in merged  # invariant tests fold in
    for g in ENGINE_SENSITIVE_GLOBS:
        assert g in merged  # engine-generic always present
    # the policy file itself is engine-sensitive (cannot be weakened by a branch)
    assert POLICY_REL in merged


def test_all_test_dirs_merges_engine_default_and_product() -> None:
    # M4: a target ADDS test locations; the engine default (literal tests/)
    # is always present - an empty test_dirs cannot weaken deleted-test
    # detection.
    pol = parse_target_policy(_MINIMAL)
    assert "src/tests/" in pol.all_test_dirs
    assert "tests/" in pol.all_test_dirs
    from dataclasses import replace

    assert "tests/" in replace(pol, test_dirs=()).all_test_dirs


def test_all_safe_merges_engine_and_product() -> None:
    pol = parse_target_policy(_MINIMAL)
    assert "web/i18n/**/*.json" in pol.all_safe_globs
    for g in ENGINE_SAFE_GLOBS:
        assert g in pol.all_safe_globs


@pytest.mark.parametrize(
    "drop",
    [
        "coverage_package",
        "sensitive_globs",
        "security_globs",
        "invariant_tests",
        "test_dirs",
        "migration_globs",
        "frontend_prefixes",
        "frontend_gate",
        "user_visible_prefixes",
        "safe_globs",
    ],
)
def test_missing_key_fails_closed(drop: str) -> None:
    lines = [ln for ln in _MINIMAL.strip().splitlines() if not ln.startswith(f"{drop} ")]
    with pytest.raises(TargetPolicyError, match="missing keys"):
        parse_target_policy("\n".join(lines))


def test_invalid_toml_fails_closed() -> None:
    with pytest.raises(TargetPolicyError, match="invalid TOML"):
        parse_target_policy("this is = = not toml")


def test_wrong_type_fails_closed() -> None:
    bad = _MINIMAL.replace('sensitive_globs = ["acme/models.py"]', 'sensitive_globs = "oops"')
    with pytest.raises(TargetPolicyError, match="must be a list of strings"):
        parse_target_policy(bad)


def test_load_from_working_tree(tmp_path: Path) -> None:
    (tmp_path / TARGET_DIR_NAME).mkdir()
    (tmp_path / POLICY_REL).write_text(_MINIMAL, encoding="utf-8")
    pol = load_target_policy(tmp_path)
    assert pol.coverage_package == "acme"


def test_load_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(TargetPolicyError, match="missing"):
        load_target_policy(tmp_path)


def test_load_from_trusted_ref_uses_git_show(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def _fake_show(repo: Path, spec: str) -> str:
        seen["spec"] = spec
        return _MINIMAL

    pol = load_target_policy(tmp_path, ref="trustedsha", git_show=_fake_show)
    assert pol.coverage_package == "acme"
    # reads <ref>:<policy path>, never the branch working tree
    assert seen["spec"] == f"trustedsha:{POLICY_REL}"


def test_shipped_laddy_policy_is_dogfood_specific() -> None:
    # <root>/.laddy/policy.toml is laddy's OWN target-side policy (dogfooding),
    # not a generic template. Pin the load-bearing dogfood invariants so a
    # careless edit (e.g. reverting to the myapp template) is caught.
    root = Path(__file__).resolve().parent.parent
    pol = parse_target_policy((root / POLICY_REL).read_text(encoding="utf-8"))
    assert pol.coverage_package == "orchestrator"  # laddy's python package
    # the trust-boundary code is stop-before-merge (security), never auto-merged
    assert "orchestrator/local_merge.py" in pol.security_globs
    assert "orchestrator/testgate.py" in pol.security_globs
    assert "orchestrator/target_policy.py" in pol.security_globs
    # laddy has no product surface beyond the engine, no frontend, no migrations
    assert pol.sensitive_globs == ()
    assert pol.frontend_prefixes == ()
    assert pol.migration_globs == ()


# M5 (C2+C3 audit): a target's safe_globs feed the L1 no-review auto-merge
# lane, so the engine validates them at PARSE time - a glob that could match a
# non-inert file (code, scripts, config without an inert extension constraint)
# fails the whole policy closed instead of silently widening L1 to cover code.


@pytest.mark.parametrize(
    "glob",
    [
        "**/*.py",  # the audit's repro: arbitrary python rides L1
        "*.PY",  # matching is casefolded (H8), so validation must be too
        "src/**",  # no extension constraint: fnmatch '*' matches src/evil.py
        "*.sh",
        "web/**/*.ts",
        "conftest.py",
        "Makefile",
        "*.json*",  # trailing wildcard: could match .jsonnet etc. - fail closed
        "locales/*",  # directory allowlist, not an extension allowlist
        ".md",  # no stem: a literal dotfile named '.md', not an extension rule
    ],
)
def test_non_inert_safe_glob_fails_closed(glob: str) -> None:
    bad = _MINIMAL.replace(
        'safe_globs = ["web/i18n/**/*.json"]', f'safe_globs = ["{glob}"]'
    )
    with pytest.raises(TargetPolicyError, match="safe_globs"):
        parse_target_policy(bad)


@pytest.mark.parametrize(
    "glob",
    [
        "web/i18n/**/*.json",  # the documented use case: i18n catalogues
        "data/**/*.csv",
        "locales/*.PO",  # casefolded: gettext catalogues in any case
        "notes/*.txt",
        "docs/extra/*.md",
    ],
)
def test_inert_safe_glob_is_accepted(glob: str) -> None:
    ok = _MINIMAL.replace(
        'safe_globs = ["web/i18n/**/*.json"]', f'safe_globs = ["{glob}"]'
    )
    assert glob in parse_target_policy(ok).safe_globs


def test_engine_safe_globs_satisfy_the_inert_rule() -> None:
    # the engine's own L1 allowlist must obey the same inert-extension contract
    # it enforces on targets (drift pin).
    for g in ENGINE_SAFE_GLOBS:
        stem, sep, ext = g.casefold().rpartition(".")
        assert sep and stem and ext in INERT_SAFE_EXTENSIONS, g


def test_dump_target_policy_round_trips() -> None:
    # the serializer (used by fakes.write_policy_toml) must round-trip through
    # the parser for every policy - a rich sample and the minimal fixture.
    for pol in (TargetPolicy.myapp(), parse_target_policy(_MINIMAL)):
        assert parse_target_policy(dump_target_policy(pol)) == pol
