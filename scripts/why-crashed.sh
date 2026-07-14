#!/usr/bin/env bash
# why-crashed.sh - rychlá diagnostika po pádu/ukončení claude (nebo čehokoliv)
# Ukáže: OOM zabití z kernel logu, aktuální paměť + swap.
set -u

echo "======================================================================"
echo " OOM killer - zabité procesy (kernel log, čitelné timestampy)"
echo "======================================================================"
if oom=$(dmesg -T 2>/dev/null | grep -iE "killed process|out of memory"); then
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
