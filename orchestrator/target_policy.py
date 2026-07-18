"""Per-target policy configuration (M1: the engine is target-agnostic).

``laddy`` is a standalone engine that holds no product code of its own; it is
pointed at a **target** project (e.g. ``myapp``). Everything the merge policy
and the gate need that is SPECIFIC to that target - which paths are sensitive
or security-critical, the coverage package, the frontend build, the invariant
tests - lives in the target's own ``<target>/.laddy/policy.toml`` (mirror of
``.laddy/{docker,security}``), NOT in the engine's Python.

Generic globs that are the same for every target (secrets, agent config, the
engine's own surfaces, the gate infra, and this config file itself) stay as
ENGINE constants here so a target cannot weaken them: ``.laddy/policy.toml`` is
itself an engine-sensitive path, so any edit to it is L3 (human-gated), and the
off-VPS merge_check loads it from TRUSTED main rather than the branch.

There is deliberately NO myapp fallback in :func:`load`: a target that ships no
``policy.toml`` fails closed (``TargetPolicyError``) rather than silently
inheriting another project's sensitive-path list - the exact fail-open M1 fixes.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator import TARGET_DIR_NAME

POLICY_REL = f"{TARGET_DIR_NAME}/policy.toml"


class TargetPolicyError(ValueError):
    """Missing or malformed per-target policy configuration."""


# --- Engine-generic globs (same for every target; NOT overridable by the toml) ---
#
# Secrets, agent-config surface (C2), supply chain, CI, the engine's own code
# (laddy-as-target), the containerized gate infra, and this policy file itself.
# A target's toml can only ADD product paths on top of these - never remove one.
ENGINE_SENSITIVE_GLOBS: tuple[str, ...] = (
    # Deploy / secret config.
    ".env*",
    "**/.env*",
    # Per-node engine config (H-D7-1): env.local / env.vps carry PYTHON_BIN,
    # *_COMMANDS, CLAUDE_CMD, ... and are `set -a; source`d on the TRUSTED
    # machine (merge-verified.sh / push-hub.sh) - i.e. any shell in them runs as
    # the Director. Their names have NO leading dot, so `.env*` above misses
    # them; classify them sensitive so an untracked-turned-tracked env.* routes
    # L3 (human sees it) instead of L2 auto-merge. The only tracked matches are
    # the harmless env.local.example / env.vps.example (also L3, fine).
    "env.*",
    "**/env.*",
    # Agent-config surface: hooks / MCP servers / steering (C2). Executes host
    # commands when the local review panel's claude/codex loads them. Nested
    # variants are flagged too (H7): the CLIs auto-ingest steering/MCP config
    # from subdirectories they descend into, not just the repo root.
    ".claude/*",
    ".claude/**/*",
    "**/.claude/*",
    "**/.claude/**/*",
    ".mcp.json",
    "**/.mcp.json",
    ".codex/*",
    ".codex/**/*",
    "**/.codex/*",
    "**/.codex/**/*",
    "CLAUDE.md",
    "**/CLAUDE.md",
    "CLAUDE.local.md",
    "**/CLAUDE.local.md",
    "AGENTS.md",
    "**/AGENTS.md",
    "GEMINI.md",
    "**/GEMINI.md",
    # CI config.
    ".github/*",
    ".github/**/*",
    # Supply chain: a dependency bump / lockfile change is the main realistic
    # attack vector (trust-model S9). Manifests AND lockfiles, at any depth
    # (M6): a dependency injected into nested backend/requirements.txt or a
    # lockfile rides the same vector as the root manifest.
    "requirements*.txt",
    "**/requirements*.txt",
    "pyproject.toml",
    "**/pyproject.toml",
    "Pipfile",
    "**/Pipfile",
    "Pipfile.lock",
    "**/Pipfile.lock",
    "poetry.lock",
    "**/poetry.lock",
    "uv.lock",
    "**/uv.lock",
    "package.json",
    "**/package.json",
    "package-lock.json",
    "**/package-lock.json",
    "pnpm-lock.yaml",
    "**/pnpm-lock.yaml",
    "yarn.lock",
    "**/yarn.lock",
    # Trusted-tool config a branch can plant to make a gate step lie (D2). Each
    # is AUTO-DISCOVERED and honored by a tool the gate runs, yet escaped the
    # sensitive set, so it rode L2 auto-merge unclassified:
    #   - conftest.py: pytest auto-loads it and a collection hook
    #     (pytest_sessionfinish -> session.exitstatus = 0) forges a green suite.
    #     Classified L3 so a human reviews the collection-hook diff; the gate
    #     itself cannot restore it (legit fixture conftests) nor tell a malicious
    #     hook from a benign one, so L3 IS the boundary (H-D2-1).
    #   - pytest.ini / tox.ini / setup.cfg: pytest config that can re-home the
    #     runner / rewrite addopts (H-D2-1).
    #   - .semgrepignore / .gitleaks.toml / .gitleaksignore: scanner ignore/allow
    #     config the scan step honors; the LOAD-BEARING fix is stripping them in
    #     the gate (testgate._containerized) so the scan cannot run vacuously -
    #     classification here is the SECOND layer (H-D2-2, H-D2-3).
    #   - .coveragerc: coverage.py (via pytest-cov) auto-discovers a dedicated
    #     .coveragerc in the gate's cwd; a branch `[run]\nomit = <its changed
    #     files>` drops those from coverage.xml so diff-cover --fail-under=90
    #     passes vacuously (the same config in setup.cfg/tox.ini/pyproject.toml is
    #     already covered above). Same class as the scanner configs: the
    #     LOAD-BEARING fix is neutralizing it in the gate, classification here is
    #     the SECOND layer.
    "conftest.py",
    "**/conftest.py",
    "pytest.ini",
    "**/pytest.ini",
    "tox.ini",
    "**/tox.ini",
    "setup.cfg",
    "**/setup.cfg",
    ".semgrepignore",
    "**/.semgrepignore",
    ".semgrep/*",
    ".semgrep/**/*",
    "**/.semgrep/*",
    "**/.semgrep/**/*",
    ".gitleaks.toml",
    "**/.gitleaks.toml",
    ".gitleaksignore",
    "**/.gitleaksignore",
    ".coveragerc",
    "**/.coveragerc",
    # Engine surfaces when laddy itself is the target branch (repo_laddy):
    # post-split the engine's own code lives at the branch REPO ROOT.
    "orchestrator/*",
    "orchestrator/**/*",
    "scripts/*",
    "prompts/*",
    "roles/*",
    "oracle/*",
    "monitoring/*",
    "docker/*",
    "security/*",
    "skills/*",
    # The gate infra + oracle data + THIS policy file, in the target's agent dir.
    # policy.toml is engine-sensitive so a branch cannot weaken its OWN
    # classification: editing it is L3, and merge_check reads it from trusted main.
    f"{TARGET_DIR_NAME}/docker/*",
    f"{TARGET_DIR_NAME}/docker/**/*",
    f"{TARGET_DIR_NAME}/security/*",
    f"{TARGET_DIR_NAME}/security/**/*",
    f"{TARGET_DIR_NAME}/oracle/*",
    POLICY_REL,
)

# Inert-extension allowlist shared by every target (markdown only). Product data
# catalogues (i18n JSON, etc.) are added per-target via ``safe_globs``.
ENGINE_SAFE_GLOBS: tuple[str, ...] = ("*.md", "**/*.md")

# Extensions a target's ``safe_globs`` may route to the L1 no-review lane (M5).
# Deliberately tiny - genuinely inert data/document formats only. JSON is here
# for the documented use case (i18n catalogues); executable-adjacent JSON like
# package.json / .mcp.json is engine-sensitive, so L3 wins before L1 is even
# considered. Deliberately NOT here: yaml/toml/ini (config that programs act
# on), svg/html (can embed scripts), and anything code-shaped. A target that
# needs more falls back to L2, where the agents gate it.
INERT_SAFE_EXTENSIONS: tuple[str, ...] = ("md", "txt", "json", "csv", "po")

# Where test files live (path prefixes). The engine default always applies -
# a target's ``test_dirs`` only ADDS locations (src/tests/, frontend/__tests__/,
# ...), it can never remove literal tests/ from deleted-test detection (M4).
ENGINE_TEST_DIRS: tuple[str, ...] = ("tests/",)

_REQUIRED_KEYS: tuple[str, ...] = (
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
)


@dataclass(frozen=True)
class TargetPolicy:
    """Target-specific policy inputs. ``sensitive_globs`` / ``safe_globs`` are
    the PRODUCT additions; the effective lists (:attr:`all_sensitive_globs`,
    :attr:`all_safe_globs`) merge in the engine-generic constants."""

    coverage_package: str
    sensitive_globs: tuple[str, ...]
    security_globs: tuple[str, ...]
    invariant_tests: tuple[str, ...]
    test_dirs: tuple[str, ...]
    migration_globs: tuple[str, ...]
    frontend_prefixes: tuple[str, ...]
    frontend_gate: str
    user_visible_prefixes: tuple[str, ...]
    safe_globs: tuple[str, ...]

    @property
    def all_sensitive_globs(self) -> tuple[str, ...]:
        """Engine-generic + product sensitive globs + invariant tests (a changed
        invariant test is a stop-before-merge condition, trust-model S8)."""
        return ENGINE_SENSITIVE_GLOBS + self.sensitive_globs + self.invariant_tests

    @property
    def all_safe_globs(self) -> tuple[str, ...]:
        """Engine-generic (markdown) + product safe-by-construction globs."""
        return ENGINE_SAFE_GLOBS + self.safe_globs

    @property
    def all_test_dirs(self) -> tuple[str, ...]:
        """Engine default (literal tests/) + the target's declared test
        locations. Additive only: a target cannot weaken deleted-test
        detection by leaving ``test_dirs`` empty (M4)."""
        return ENGINE_TEST_DIRS + self.test_dirs

    @classmethod
    def myapp(cls) -> TargetPolicy:
        """A representative sample target policy (auth / payments / frontend /
        migration surface) used as the test fixture for the classification logic.
        Originally the values hardcoded in policy.py / testgate.py before M1. It
        is NO LONGER the shipped ``.laddy/policy.toml`` (that is now laddy's own
        dogfood policy); ``fakes.write_policy_toml`` serializes THIS into test
        repos so the oracle/merge tests keep a rich sample to classify."""
        return cls(
            coverage_package="myapp",
            sensitive_globs=(
                "myapp/models.py",
                "myapp/infrastructure/runtime_db.py",
                "alembic/*",
                "alembic/**/*",
                "cloudbuild.yaml",
                "scripts/release*.ps1",
                "docker-entrypoint.sh",
                "Dockerfile",
                "firebase.json",
                ".firebaserc",
            ),
            security_globs=(
                "myapp/api/routers/auth*.py",
                "myapp/api/services/auth*.py",
                "myapp/infrastructure/auth_repo.py",
                "myapp/application/access.py",
                "myapp/api/routers/sessions.py",
                "myapp/api/routers/payments.py",
                "myapp/api/routers/*webhook*.py",
                "myapp/application/payments/*",
                "myapp/application/payments/**/*",
                "myapp/infrastructure/payments/*",
                "myapp/infrastructure/payments/**/*",
            ),
            invariant_tests=(
                "tests/test_architecture_contracts.py",
                "tests/test_append_only_session_history.py",
                "tests/test_inbox_audit_append_only.py",
                "tests/test_admin_user_notes_append_only.py",
                "tests/test_player_challenge_answer_append_only.py",
                "tests/test_invariants_append_only_game_edit_event.py",
                "tests/test_purchase_payment_received_at_immutable.py",
            ),
            test_dirs=("src/tests/", "frontend/__tests__/"),
            migration_globs=("alembic/*", "alembic/**/*"),
            frontend_prefixes=("frontend/", "apps/", "packages/"),
            frontend_gate=(
                "pnpm install --frozen-lockfile && pnpm -r --if-present build "
                "&& pnpm -r --if-present test && cd frontend && npx tsc --noEmit"
            ),
            user_visible_prefixes=("frontend/", "apps/"),
            safe_globs=(
                "frontend/src/i18n/locales/**/*.json",
                "frontend/**/locales/**/*.json",
                "apps/**/locales/**/*.json",
            ),
        )


def _as_str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise TargetPolicyError(f"{POLICY_REL}: '{key}' must be a string")
    return value


def _as_str_tuple(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TargetPolicyError(f"{POLICY_REL}: '{key}' must be a list of strings")
    return tuple(value)


def _as_inert_safe_globs(value: object) -> tuple[str, ...]:
    """Validate ``safe_globs`` entries against the inert-extension allowlist (M5).

    ``safe_globs`` feed the L1 no-review auto-merge lane, so a target may only
    ADD inert data catalogues - never widen L1 to cover code. Each glob must
    end in a literal ``.<ext>`` with ``ext`` on :data:`INERT_SAFE_EXTENSIONS`:
    a glob without that constraint (``src/**`` - fnmatch's ``*`` crosses ``/``
    and matches ``src/evil.py``), with a code extension (``**/*.py``), or with
    a wildcard in the suffix (``*.json*``) is rejected at PARSE time, failing
    the whole policy closed. Loud beats silent: match-time skipping would hide
    the misconfiguration while the Director believes the lane exists.
    Casefolded like the matcher (H8), so ``*.PY`` is still code and ``*.PO``
    is still a catalogue.
    """
    globs = _as_str_tuple(value, "safe_globs")
    for g in globs:
        stem, sep, ext = g.casefold().rpartition(".")
        if not sep or not stem or ext not in INERT_SAFE_EXTENSIONS:
            raise TargetPolicyError(
                f"{POLICY_REL}: safe_globs entry {g!r} could match a non-inert "
                "file; L1 globs must end in a literal inert extension "
                f"({', '.join('.' + e for e in INERT_SAFE_EXTENSIONS)})"
            )
    return globs


def parse_target_policy(text: str) -> TargetPolicy:
    """Parse ``policy.toml`` content into a :class:`TargetPolicy`.

    Every key is required (no silent default), errors propagate: a target must
    declare its whole policy or fail closed.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TargetPolicyError(f"{POLICY_REL}: invalid TOML: {exc}") from exc
    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise TargetPolicyError(f"{POLICY_REL}: missing keys: {', '.join(missing)}")
    return TargetPolicy(
        coverage_package=_as_str(data["coverage_package"], "coverage_package"),
        sensitive_globs=_as_str_tuple(data["sensitive_globs"], "sensitive_globs"),
        security_globs=_as_str_tuple(data["security_globs"], "security_globs"),
        invariant_tests=_as_str_tuple(data["invariant_tests"], "invariant_tests"),
        test_dirs=_as_str_tuple(data["test_dirs"], "test_dirs"),
        migration_globs=_as_str_tuple(data["migration_globs"], "migration_globs"),
        frontend_prefixes=_as_str_tuple(data["frontend_prefixes"], "frontend_prefixes"),
        frontend_gate=_as_str(data["frontend_gate"], "frontend_gate"),
        user_visible_prefixes=_as_str_tuple(
            data["user_visible_prefixes"], "user_visible_prefixes"
        ),
        safe_globs=_as_inert_safe_globs(data["safe_globs"]),
    )


def dump_target_policy(policy: TargetPolicy) -> str:
    """Serialize a :class:`TargetPolicy` back to ``policy.toml`` text.

    Round-trips with :func:`parse_target_policy`. Used by tests to seed a repo
    from an in-code sample policy (``fakes.write_policy_toml``) without coupling
    to whatever the shipped ``.laddy/policy.toml`` happens to be.
    """

    def _s(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _arr(items: tuple[str, ...]) -> str:
        return "[" + ", ".join(_s(item) for item in items) + "]"

    return "\n".join(
        (
            f"coverage_package = {_s(policy.coverage_package)}",
            f"sensitive_globs = {_arr(policy.sensitive_globs)}",
            f"security_globs = {_arr(policy.security_globs)}",
            f"invariant_tests = {_arr(policy.invariant_tests)}",
            f"test_dirs = {_arr(policy.test_dirs)}",
            f"migration_globs = {_arr(policy.migration_globs)}",
            f"frontend_prefixes = {_arr(policy.frontend_prefixes)}",
            f"frontend_gate = {_s(policy.frontend_gate)}",
            f"user_visible_prefixes = {_arr(policy.user_visible_prefixes)}",
            f"safe_globs = {_arr(policy.safe_globs)}",
            "",
        )
    )


def load_target_policy(
    repo_root: Path,
    ref: str | None = None,
    git_show: Callable[[Path, str], str] | None = None,
) -> TargetPolicy:
    """Load ``<repo>/.laddy/policy.toml``.

    ``ref`` reads the file from a TRUSTED git ref (e.g. local main) instead of
    the working tree - the off-VPS merge_check uses this so a branch cannot ship
    a weakened policy and have the recompute honor it. ``git_show`` is injectable
    for tests; it defaults to ``git show <ref>:<POLICY_REL>`` run in ``repo_root``.
    """
    if ref is None:
        path = repo_root / POLICY_REL
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TargetPolicyError(
                f"missing {POLICY_REL} in {repo_root} - a target must declare its "
                "policy (there is no myapp fallback); seed it from the engine template"
            ) from exc
        return parse_target_policy(text)
    show = git_show or _git_show
    return parse_target_policy(show(repo_root, f"{ref}:{POLICY_REL}"))


def _git_show(repo_root: Path, spec: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(repo_root), "show", spec],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise TargetPolicyError(
            f"cannot read {spec} from trusted ref: {proc.stderr.strip()}"
        )
    return proc.stdout
