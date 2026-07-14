from __future__ import annotations

import json

from loop_monitor.classify import ProcessIdentity, classify_process, inherited_identity
from loop_monitor.procfs import ProcessSample, ranked_processes


def test_classification_never_returns_raw_command_line_or_secret() -> None:
    secret = "token-super-secret"
    identity = classify_process(
        comm="python3",
        argv=("python3", "-m", "pytest", "tests/test_api.py", f"--token={secret}"),
        cwd="/root/agent-work/myapp-task",
    )

    serialized = json.dumps(identity.to_json())

    assert identity.operation == "pytest"
    assert identity.repo == "myapp-task"
    assert secret not in serialized
    assert "test_api.py" not in serialized


def test_pytest_descendant_is_counted_as_worker() -> None:
    root = ProcessIdentity(category="test", operation="pytest", repo="myapp")
    child = ProcessIdentity(category="other", operation="other", repo="myapp")
    nested_pytest = ProcessIdentity(category="test", operation="pytest", repo="myapp")

    assert inherited_identity(child, root).operation == "pytest_worker"
    assert inherited_identity(nested_pytest, root).operation == "pytest_worker"


def test_shell_watcher_that_mentions_pytest_is_not_an_active_test() -> None:
    identity = classify_process(
        comm="bash",
        argv=("/bin/bash", "-c", 'while pgrep -f "pytest tests/x"; do sleep 10; done'),
        cwd="/root/myapp",
    )

    assert identity.operation == "other"


def test_monitor_wrapper_is_not_counted_as_monitor_process() -> None:
    wrapper = classify_process(
        comm="timeout",
        argv=("timeout", "46s", "python3", "-m", "loop_monitor", "collect"),
        cwd="/root/myapp",
    )
    actual = classify_process(
        comm="python3",
        argv=("python3", "-m", "loop_monitor", "collect"),
        cwd="/opt/loop-monitor",
    )

    assert wrapper.operation == "other"
    assert actual.operation == "loop_monitor"


def test_node_entrypoints_distinguish_build_dev_and_frontend_tests() -> None:
    vite_build = classify_process(
        comm="node",
        argv=("node", "/repo/node_modules/vite/bin/vite.js", "build"),
        cwd="/repo/frontend",
    )
    vite_dev = classify_process(
        comm="node",
        argv=("node", "/repo/node_modules/vite/bin/vite.js"),
        cwd="/repo/frontend",
    )
    vitest = classify_process(
        comm="node",
        argv=("node", "/repo/node_modules/vitest/vitest.mjs", "run"),
        cwd="/repo/frontend",
    )

    assert vite_build.operation == "build"
    assert vite_dev.operation == "frontend_dev"
    assert vitest.operation == "frontend_test"


def test_claude_helpers_are_not_counted_as_agent_sessions() -> None:
    helper = classify_process(
        comm="claude",
        argv=("claude", "bg-spare", "--bg-spare", "/tmp/socket"),
        cwd="/root",
    )
    agent = classify_process(
        comm="2.1.207",
        argv=("/root/.local/share/claude/versions/2.1.207", "--resume", "opaque"),
        cwd="/root/myapp",
    )

    assert helper.operation == "claude_helper"
    assert helper.is_agent_helper is True
    assert agent.operation == "claude_agent"
    assert agent.is_agent_helper is False


def test_ranked_process_detail_has_only_safe_identity() -> None:
    sample = ProcessSample(
        pid=7,
        ppid=1,
        start_ticks=10,
        comm="python3",
        identity=ProcessIdentity(
            category="backend", operation="backend_server", repo="myapp"
        ),
        cpu_ticks=20,
        rss_bytes=1024,
        read_bytes=0,
        write_bytes=0,
        cpu_pct=50,
    )

    assert ranked_processes([sample], 20) == [
        {
            "pid": 7,
            "ppid": 1,
            "comm": "python3",
            "category": "backend",
            "operation": "backend_server",
            "repo": "myapp",
            "cpu_pct": 50,
            "rss_bytes": 1024,
            "read_bps": 0.0,
            "write_bps": 0.0,
        }
    ]
