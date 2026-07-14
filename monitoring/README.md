# Lightweight VPS monitoring

This module observes the development VPS used by the `.laddy` agent loop. It
does not set CPU, memory, Docker, systemd or cgroup limits.

## Design

One Python process (standard library only) writes a compact JSONL sample every
15 seconds. It reads host counters from `/proc`, scans process metadata, polls
running-container stats through `/var/run/docker.sock`, and keeps one Docker
event stream open. A full container inspect happens only at startup, after a
lifecycle/state change, or every five minutes as a backstop.

Normal samples contain aggregates. A bounded process list is written only for
an incident (high CPU/load, low available RAM, swap I/O, host/container OOM,
container die/restart/unhealthy, or sustained I/O pressure), at most every 30
seconds unless the event is critical. Files rotate daily and expire after 21
days.

The collector reads argv only in memory to classify `python`/`node` processes.
It never returns or persists argv, environment variables, prompts, transcript
paths, hook messages, tokens or container environment/config. Detailed rows
contain PID, PPID, `comm`, a closed operation category, CPU/RSS/I/O rates, a
safe task slug and repository basename.

Logical Claude and Codex subagents are counted from `SubagentStart` and
`SubagentStop` hooks. Top-level CLI sessions are counted independently from
the process tree. The hook receiver accepts only vendor, event, opaque IDs,
agent type and repository basename; all other hook fields are discarded.

## Metrics

- Host: CPU, load 1/5/15, CPU count, total/available/used RAM, cache, buffers,
  dirty pages, total/used swap.
- VM: cumulative and delta swap-in/swap-out pages, major faults and OOM kills.
- Disk/pressure: root-device read/write B/s and utilization, CPU/memory/I/O PSI.
- Processes: counts and aggregate CPU/RSS/read/write rates by operation;
  top resource users in incident snapshots.
- Agents: parallel Claude/Codex top-level sessions, helper processes and exact
  logical subagents while hooks are active.
- Tests/builds: pytest roots, pytest/xdist descendants, backend, frontend,
  build, database and container-runtime operations.
- Docker: CPU, memory, PIDs and block I/O per running container; state, health,
  exit code, restart count and OOMKilled; lifecycle events.

## Install on the VPS

From the current repository clone:

```bash
cd /root/myapp-trusted
chmod +x .laddy/monitoring/install.sh .laddy/monitoring/uninstall.sh
./.laddy/monitoring/install.sh
```

The installer merges two lifecycle handlers into `/root/.claude/settings.json`
and `/root/.codex/hooks.json`; it preserves unrelated hooks and creates a
one-time `*.pre-loop-monitor` backup. Codex requires interactive trust for
new command hooks: start `codex`, run `/hooks`, inspect the exact commands and
trust them once. Until then, process-based Codex session counts still work but
logical Codex subagent counts remain zero.

Verify:

```bash
systemctl status loop-monitor --no-pager
/opt/loop-monitor/bin/loop-monitor check
journalctl -u loop-monitor --since '-10 min' --no-pager
find /var/lib/loop-monitor -type f -printf '%p %s bytes\n'
```

Exercise a hook after the service is running:

```bash
printf '%s\n' '{"hook_event_name":"SubagentStart","session_id":"verify","agent_id":"one","agent_type":"test","cwd":"/root/myapp-trusted"}' \
  | /opt/loop-monitor/bin/loop-monitor hook --vendor claude
printf '%s\n' '{"hook_event_name":"SubagentStop","session_id":"verify","agent_id":"one","agent_type":"test","cwd":"/root/myapp-trusted"}' \
  | /opt/loop-monitor/bin/loop-monitor hook --vendor claude
tail -n 2 /var/lib/loop-monitor/events/"$(date -u +%F)".jsonl | jq .
```

## Analyze a spike

The time may include a timezone; a timestamp without one uses the server's
local timezone:

```bash
/opt/loop-monitor/bin/loop-monitor report \
  --at '2026-07-12T20:29:00+02:00' --window-minutes 5
```

The report shows the window's CPU/RAM/swap/I/O extrema, concurrent agents,
subagents, tests and workers at the nearest sample, active operation groups,
incident top processes and Docker lifecycle events. Add `--json` for raw
machine-readable evidence.

Example interpretation:

```text
Peak CPU: 92.4% at 2026-07-13T01:10:30Z
Agents: Claude sessions=2, Codex sessions=1, logical subagents=3, parallel total=6
Tests: pytest roots=1, xdist/workers=6
Incident details: high_cpu, high_load
  pid=... python op=pytest_worker cpu=96.1% rss=310.0 MiB
```

This establishes that six agent contexts and six pytest workers overlapped,
and identifies the worker consuming the most resources. The same incident
record includes swap deltas and the preceding container events.

## Measure monitoring overhead

After at least an hour, and again after 24 hours:

```bash
/opt/loop-monitor/bin/loop-monitor overhead --hours 1
/opt/loop-monitor/bin/loop-monitor overhead --hours 24
systemctl show loop-monitor -p CPUUsageNSec -p MemoryCurrent -p IOReadBytes -p IOWriteBytes
du -sh /var/lib/loop-monitor
```

Acceptance targets are average CPU below 1% of the host, RSS below 100 MiB and
projected writes ideally below 50 MiB/day. Tune only the documented interval,
detail interval or top-process limit if measured values exceed the target.

Pre-deployment smoke measurement in the development sandbox (three real
`/proc` samples over 31 seconds, no running Docker containers) measured 0.11%
average process CPU, 20 MiB maximum RSS and a projected 10.6 MiB/day of base
samples. `/usr/bin/time -v` observed 0.40 CPU seconds total. Repeat the same
measurement on the six-core VPS under real Docker/test load before accepting
the rollout; incident records and container count increase the write rate.

## Retire the old temporary loop

The old PID `820435` is a manually launched infinite shell loop that invokes
`docker stats` every 15 seconds and appends to an unrotated temporary file. It
is not a service and will disappear on reboot. Keep it during initial
comparison, then after the new monitor has produced valid samples for at least
an hour:

```bash
pgrep -f '/tmp/gate-diag/stats.log'
kill 820435
pgrep -f '/tmp/gate-diag/stats.log' || echo 'old loop stopped'
```

Do not delete `/tmp/gate-diag/stats.log` until its historical data is no
longer needed. If PID 820435 has changed, inspect the PID returned by `pgrep`
and kill that exact process instead.

## Rollback

```bash
cd /root/myapp-trusted
./.laddy/monitoring/uninstall.sh
```

This stops/disables the service, removes only its exact hook handlers and
removes `/opt/loop-monitor`. Collected evidence remains in
`/var/lib/loop-monitor`. Use `uninstall.sh --purge-data` only when that retained
history should also be deleted. If needed, restart the old temporary loop from
its original command; rollback never changes Docker, PostgreSQL or sysstat.
