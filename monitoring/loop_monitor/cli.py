"""Command-line entry points for collection, hooks and analysis."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from loop_monitor.collector import Monitor, run_service
from loop_monitor.config import MonitorConfig
from loop_monitor.docker_api import DockerAPI, DockerUnavailable
from loop_monitor.events import emit_hook_event
from loop_monitor.report import build_report, json_report, overhead_report, parse_time
from loop_monitor.report_path import ReportPathError, render_markdown, write_report


def _config() -> MonitorConfig:
    try:
        return MonitorConfig.from_env()
    except ValueError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc


def _hook(args: argparse.Namespace) -> int:
    config = _config()
    raw = sys.stdin.buffer.read(65_537)
    emit_hook_event(args.vendor, config.socket_path, raw)
    # Codex SubagentStop requires valid JSON on stdout. An empty object is a
    # no-op for both vendors and prevents monitoring from changing behavior.
    sys.stdout.write("{}\n")
    return 0


def _check(config: MonitorConfig) -> int:
    result: dict[str, object] = {
        "data_dir": str(config.data_dir),
        "data_dir_writable": os.access(config.data_dir, os.W_OK)
        if config.data_dir.exists()
        else os.access(config.data_dir.parent, os.W_OK),
        "procfs": Path("/proc/stat").is_file(),
        "docker_socket": Path("/var/run/docker.sock").exists(),
        "hook_socket": config.socket_path.exists(),
    }
    try:
        containers = DockerAPI().request_json("/containers/json?all=1")
        result["docker_api"] = True
        result["docker_containers"] = (
            len(containers) if isinstance(containers, list) else 0
        )
    except DockerUnavailable:
        result["docker_api"] = False
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["procfs"] and result["data_dir_writable"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loop-monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("collect", help="run the long-lived collector")
    hook = subparsers.add_parser("hook", help="receive one Claude/Codex hook on stdin")
    hook.add_argument("--vendor", required=True, choices=("claude", "codex"))
    report = subparsers.add_parser("report", help="analyze a time window")
    report.add_argument("--at", help="ISO timestamp; defaults to now")
    report.add_argument("--window-minutes", type=float, default=5.0)
    report.add_argument("--json", action="store_true", dest="as_json")
    report.add_argument(
        "--out",
        type=Path,
        help="write the report to this .md file (guarded) instead of stdout",
    )
    report.add_argument(
        "--out-root",
        type=Path,
        help="allowed output root for --out (defaults to the monitor data_dir)",
    )
    report.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing regular file at --out",
    )
    overhead = subparsers.add_parser("overhead", help="measure monitor overhead")
    overhead.add_argument("--hours", type=float, default=24.0)
    subparsers.add_parser("check", help="verify local prerequisites")
    subparsers.add_parser("once", help="write one sample (diagnostics only)")
    args = parser.parse_args(argv)
    config = _config()
    if args.command == "collect":
        run_service(config)
        return 0
    if args.command == "hook":
        return _hook(args)
    if args.command == "check":
        return _check(config)
    if args.command == "once":
        monitor = Monitor(config)
        monitor.start()
        try:
            print(json.dumps(monitor.collect_once(), indent=2, sort_keys=True))
        finally:
            monitor.stop()
        return 0
    if args.command == "overhead":
        print(overhead_report(config.data_dir, args.hours))
        return 0
    if args.command == "report":
        if args.out is not None and args.as_json:
            report.error("--out cannot be combined with --json")
        at = parse_time(args.at) if args.at else time.time()
        window = max(0.25, args.window_minutes) * 60
        if args.out is not None:
            out_root = args.out_root if args.out_root is not None else config.data_dir
            body = build_report(config.data_dir, at, window)
            try:
                write_report(
                    render_markdown(body), args.out, out_root, force=args.force
                )
            except ReportPathError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            return 0
        output = (
            json_report(config.data_dir, at, window)
            if args.as_json
            else build_report(config.data_dir, at, window)
        )
        print(output)
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
