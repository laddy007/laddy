#!/usr/bin/env bash
# colorize-log.sh -- stdin filter that highlights dev-loop logs:
#   role names (developer / rw1 / rw2 / senior / explorer / ...)  -> blue
#   file names (path or bare, by extension)                       -> yellow
#
# Usage:  ssh myapp-vps "tail -f ~/agent-logs/<task>.log" | ./colorize-log.sh
# (watch-vps.sh pipes through this automatically.)

ESC=$(printf '\033')
B="${ESC}[34m"   # blue
Y="${ESC}[33m"   # yellow
R="${ESC}[0m"    # reset

# -u = unbuffered (line-buffered) so a live `tail -f | colorize` streams per line.
exec sed -u -E \
  -e "s#([A-Za-z0-9_./-]+\.(astro|tsx|ts|jsx|js|mjs|cjs|py|md|json|jsonc|css|scss|sh|ps1|html|htm|yaml|yml|toml|cfg|ini|txt|sql|lock))#${Y}\1${R}#g" \
  -e "s/\b([Dd]eveloper|[Dd]ebugger|[Ee]xplorer|[Ii]nvestigator|[Vv]erify|rw1|rw2|[Ss]ecurity|[Ss]enior(-reviewer)?|[Cc]larify)\b/${B}\1${R}/g"
