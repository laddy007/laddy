"""Engine/target path split (spec §3 step 0): ENGINE_DIR is the install
root; TARGET_DIR_NAME names the in-target artifact dir and is configurable."""

import importlib
from pathlib import Path

import orchestrator


def test_engine_dir_is_repo_root():
    assert orchestrator.ENGINE_DIR == Path(orchestrator.__file__).resolve().parents[1]
    assert (orchestrator.ENGINE_DIR / "orchestrator").is_dir()


def test_target_dir_name_default():
    assert orchestrator.TARGET_DIR_NAME == ".laddy"


def test_target_dir_name_env_override(monkeypatch):
    monkeypatch.setenv("LADDY_TARGET_DIR", ".agentdir")
    importlib.reload(orchestrator)
    try:
        assert orchestrator.TARGET_DIR_NAME == ".agentdir"
    finally:
        monkeypatch.delenv("LADDY_TARGET_DIR")
        importlib.reload(orchestrator)


def test_old_names_are_gone():
    assert not hasattr(orchestrator, "AGENT_DIR_NAME")
    assert not hasattr(orchestrator, "AGENT_DIR")


def test_roles_resolve_from_engine_install():
    from orchestrator import ENGINE_DIR
    from orchestrator.run import default_roles_dir

    assert default_roles_dir() == ENGINE_DIR / "roles"
    assert (default_roles_dir() / "security.md").is_file()
