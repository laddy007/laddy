"""Idempotent installation/removal of user-level agent lifecycle hooks."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

_COMMAND = "/opt/loop-monitor/bin/loop-monitor hook --vendor {vendor}"
_EVENTS = ("SubagentStart", "SubagentStop")


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".pre-loop-monitor")
        if not backup.exists():
            shutil.copy2(path, backup)
    temporary = path.with_name(path.name + ".loop-monitor.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def _install(path: Path, vendor: str) -> bool:
    payload = _load(path)
    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: hooks must be an object")
    command = _COMMAND.format(vendor=vendor)
    changed = False
    for event in _EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"{path}: hooks.{event} must be a list")
        present = any(
            isinstance(group, dict)
            and isinstance(group.get("hooks"), list)
            and any(
                isinstance(handler, dict) and handler.get("command") == command
                for handler in group["hooks"]
            )
            for group in groups
        )
        if not present:
            groups.append(
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 2,
                        }
                    ],
                }
            )
            changed = True
    if changed:
        _write(path, payload)
    return changed


def _uninstall(path: Path, vendor: str) -> bool:
    if not path.exists():
        return False
    payload = _load(path)
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return False
    command = _COMMAND.format(vendor=vendor)
    changed = False
    for event in _EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        remaining_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                remaining_groups.append(group)
                continue
            handlers = [
                handler
                for handler in group["hooks"]
                if not (isinstance(handler, dict) and handler.get("command") == command)
            ]
            if len(handlers) != len(group["hooks"]):
                changed = True
            if handlers:
                remaining_groups.append({**group, "hooks": handlers})
        if remaining_groups:
            hooks[event] = remaining_groups
        else:
            hooks.pop(event, None)
    if changed:
        _write(path, payload)
    return changed


def configure(home: Path, uninstall: bool = False) -> dict[str, bool]:
    action = _uninstall if uninstall else _install
    return {
        "claude": action(home / ".claude" / "settings.json", "claude"),
        "codex": action(home / ".codex" / "hooks.json", "codex"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loop-monitor-hooks")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = configure(args.home, args.uninstall)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"hook configuration failed: {exc}") from exc
    action = "removed" if args.uninstall else "installed"
    for vendor, did_change in changed.items():
        state = action if did_change else "unchanged"
        print(f"{vendor}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
