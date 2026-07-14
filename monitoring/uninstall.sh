#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

PURGE_DATA=0
if [ "${1:-}" = "--purge-data" ]; then
  PURGE_DATA=1
elif [ "$#" -gt 0 ]; then
  echo "Usage: $0 [--purge-data]" >&2
  exit 2
fi

HOOK_HOME="${LOOP_MONITOR_HOOK_HOME:-/root}"
if [ -d /opt/loop-monitor/loop_monitor ]; then
  PYTHONPATH=/opt/loop-monitor /usr/bin/python3 -m loop_monitor.hook_config \
    --home "$HOOK_HOME" --uninstall || true
fi

systemctl disable --now loop-monitor.service 2>/dev/null || true
rm -f /etc/systemd/system/loop-monitor.service /etc/default/loop-monitor
rm -rf /opt/loop-monitor
systemctl daemon-reload

if [ "$PURGE_DATA" -eq 1 ]; then
  rm -rf /var/lib/loop-monitor
  echo "[uninstall] service, hooks and retained data removed"
else
  echo "[uninstall] service and hooks removed; /var/lib/loop-monitor retained"
fi
