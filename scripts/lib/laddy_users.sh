# scripts/lib/laddy_users.sh - shared LADDY_USERS parsing/validation.
#
# Source this after `source vps.conf`. Populates:
#   ENTRY[user]="ssh_alias:engine_path:project"   (associative array)
#   ORDER=(user...)                                (config order)
#
# Schema: LADDY_USERS is space-separated entries of exactly 4 colon-separated
# fields: user:ssh_alias:engine_path:project
#   - user:        unprivileged system user on the VPS (never root).
#   - ssh_alias:   ~/.ssh/config Host entry for that VPS user.
#   - engine_path: absolute path to that user's engine checkout on the VPS.
#   - project:     target project name; the user's hub lives at
#                   ~/repo_<project>/hub.git on the VPS.
# One home for this schema - both vps-onboard.sh and upgrade_laddy.sh source
# this file so the parsing can never drift between them.

laddy_parse_users() {
  declare -gA ENTRY=()
  ORDER=()
  for spec in $LADDY_USERS; do
    # charset guard also makes the ':' split safe: no field can contain ':',
    # so read's "last var takes the rest" behavior can't smuggle extra fields.
    IFS=: read -r u alias path project extra <<<"$spec"
    [ -n "$u" ] && [ -n "$alias" ] && [ -n "$path" ] && [ -n "$project" ] && [ -z "$extra" ] \
      || die "bad LADDY_USERS entry (need user:ssh_alias:engine_path:project): $spec"
    [[ "$u" =~ ^[a-zA-Z0-9._-]+$ ]] || die "LADDY_USERS entry '$spec': user contains unsafe characters"
    [[ "$alias" =~ ^[a-zA-Z0-9._-]+$ ]] || die "LADDY_USERS entry '$spec': ssh alias contains unsafe characters"
    [[ "$path" =~ ^[a-zA-Z0-9._/-]+$ ]] || die "LADDY_USERS entry '$spec': engine path contains unsafe characters"
    [[ "$path" == /* ]] || die "LADDY_USERS entry '$spec': engine path must be absolute"
    [[ "$project" =~ ^[a-zA-Z0-9._-]+$ ]] || die "LADDY_USERS entry '$spec': project contains unsafe characters"
    ENTRY["$u"]="$alias:$path:$project"
    ORDER+=("$u")
  done
  [ "${#ORDER[@]}" -gt 0 ] || die "LADDY_USERS is empty"
}
