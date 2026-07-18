#!/usr/bin/env bash
# why-crashed.sh - rychlá diagnostika po pádu/ukončení claude (nebo čehokoliv)
# Ukáže: OOM zabití z kernel logu, aktuální paměť + swap.
set -u

echo "======================================================================"
echo " OOM killer - zabité procesy (kernel log, čitelné timestampy)"
echo "======================================================================"
if dmesg_out=$(dmesg -T 2>/dev/null); then
  # Test dmesg availability FIRST (the if above), THEN grep separately - a
  # working dmesg with no OOM match must report "no OOM", not "dmesg unavailable"
  # (which is what piping dmesg|grep into the if would do, since grep's non-match
  # exit 1 is the pipeline's status).
  oom=$(printf '%s\n' "$dmesg_out" | grep -iE "killed process|out of memory")
  if [ -n "$oom" ]; then
    echo "$oom" | tail -20
    echo
    echo ">> Pokud vidíš 'Killed process ... (MainThread/claude/node)', byl to OOM."
    echo ">> Swap už je zapnutý, takže by se to opakovat nemělo - když ano, možná chce víc swapu."
  else
    echo "(žádné OOM zabití v aktuálním kernel logu - pád NEBYL kvůli paměti)"
  fi
else
  echo "(dmesg nedostupné bez oprávnění - zkus: sudo $0)"
fi

echo
echo "======================================================================"
echo " Paměť + swap teď"
echo "======================================================================"
free -h
echo
swapon --show 2>/dev/null || echo "(žádný swap aktivní!)"
