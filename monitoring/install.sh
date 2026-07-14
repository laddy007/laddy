#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_HOME="${LOOP_MONITOR_HOOK_HOME:-/root}"

install -d -m 0755 /opt/loop-monitor /opt/loop-monitor/bin
install -d -m 0750 /var/lib/loop-monitor
install -d -m 0755 /opt/loop-monitor/loop_monitor
for source in "$SCRIPT_DIR"/loop_monitor/*.py; do
  install -m 0644 "$source" "/opt/loop-monitor/loop_monitor/$(basename "$source")"
done
install -m 0755 "$SCRIPT_DIR/bin/loop-monitor" /opt/loop-monitor/bin/loop-monitor
install -m 0644 "$SCRIPT_DIR/systemd/loop-monitor.service" /etc/systemd/system/loop-monitor.service

if [ ! -e /etc/default/loop-monitor ]; then
  install -m 0644 "$SCRIPT_DIR/systemd/loop-monitor.default" /etc/default/loop-monitor
else
  echo "[install] preserving existing /etc/default/loop-monitor"
fi

PYTHONPATH=/opt/loop-monitor /usr/bin/python3 -m loop_monitor.hook_config --home "$HOOK_HOME"

systemctl daemon-reload
systemctl enable --now loop-monitor.service

echo "[install] service installed and started"
echo "[install] verify: /opt/loop-monitor/bin/loop-monitor check"
echo "[install] Codex only: open /hooks once and trust the two loop-monitor hooks"
