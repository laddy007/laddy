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
    # Agent-config surface: hooks / MCP servers / steering (C2). Executes host
    # commands when the local review panel's claude/codex loads them.
    ".claude/*",
    ".claude/**/*",
    ".mcp.json",
    ".codex/*",
    ".codex/**/*",
    "CLAUDE.md",
    "**/CLAUDE.md",
    "AGENTS.md",
    "**/AGENTS.md",
    "GEMINI.md",
    "**/GEMINI.md",
    # CI config.
    ".github/*",
    ".github/**/*",
    # Supply chain: a dependency bump / lockfile change is the main realistic
    # attack vector (trust-model S9).
    "requirements*.txt",
    "pyproject.toml",
    "package.json",
    "**/package.json",
    "pnpm-lock.yaml",
    "**/pnpm-lock.yaml",
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

_REQUIRED_KEYS: tuple[str, ...] = (
    "coverage_package",
    "sensitive_globs",
    "security_globs",
    "invariant_tests",
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

    @classmethod
    def myapp(cls) -> TargetPolicy:
        """The myapp target's policy - the values that used to be hardcoded in
        policy.py / testgate.py. Also the content of the shipped template."""
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
        migration_globs=_as_str_tuple(data["migration_globs"], "migration_globs"),
        frontend_prefixes=_as_str_tuple(data["frontend_prefixes"], "frontend_prefixes"),
        frontend_gate=_as_str(data["frontend_gate"], "frontend_gate"),
        user_visible_prefixes=_as_str_tuple(
            data["user_visible_prefixes"], "user_visible_prefixes"
        ),
        safe_globs=_as_str_tuple(data["safe_globs"], "safe_globs"),
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
