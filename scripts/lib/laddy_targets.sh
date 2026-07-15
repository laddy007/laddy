# scripts/lib/laddy_targets.sh - shared LADDY_TARGETS parsing/validation.
#
# Source this after `source local.conf`. Populates:
#   ENTRY[project]="ssh_alias:hub_path:local_repo_path"  (associative array)
#   ORDER=(project...)                                    (config order)
#
# Schema: LADDY_TARGETS is space-separated entries of exactly 4
# colon-separated fields: project:ssh_alias:hub_path:local_repo_path
#   - project:         target project name; matches the `project` field a
#                       VPS-side LADDY_USERS entry used for that same target.
#   - ssh_alias:       ~/.ssh/config Host entry that reaches the VPS user who
#                       owns this project's hub (key material lives in ssh
#                       config, never here).
#   - hub_path:        absolute path to the bare hub on the VPS, as seen by
#                       that alias (e.g. /home/laddy/repo_laddy/hub.git).
#   - local_repo_path: absolute path to this project's own trusted checkout
#                       on THIS machine (git remote gets added there, and
#                       env.local gets written there).
# One home for this schema - local-onboard.sh sources this file so a second
# script added later can reuse it without drifting.

laddy_parse_targets() {
  declare -gA ENTRY=()
  ORDER=()
  for spec in $LADDY_TARGETS; do
    # charset guard also makes the ':' split safe: no field can contain ':',
    # so read's "last var takes the rest" behavior can't smuggle extra fields.
    IFS=: read -r project alias hub path extra <<<"$spec"
    [ -n "$project" ] && [ -n "$alias" ] && [ -n "$hub" ] && [ -n "$path" ] && [ -z "$extra" ] \
      || die "bad LADDY_TARGETS entry (need project:ssh_alias:hub_path:local_repo_path): $spec"
    [[ "$project" =~ ^[a-zA-Z0-9._-]+$ ]] || die "LADDY_TARGETS entry '$spec': project contains unsafe characters"
    [[ "$alias" =~ ^[a-zA-Z0-9._-]+$ ]] || die "LADDY_TARGETS entry '$spec': ssh alias contains unsafe characters"
    [[ "$hub" =~ ^[a-zA-Z0-9._/-]+$ ]] || die "LADDY_TARGETS entry '$spec': hub path contains unsafe characters"
    [[ "$hub" == /* ]] || die "LADDY_TARGETS entry '$spec': hub path must be absolute"
    [[ "$path" =~ ^[a-zA-Z0-9._/-]+$ ]] || die "LADDY_TARGETS entry '$spec': local repo path contains unsafe characters"
    [[ "$path" == /* ]] || die "LADDY_TARGETS entry '$spec': local repo path must be absolute"
    ENTRY["$project"]="$alias:$hub:$path"
    ORDER+=("$project")
  done
  [ "${#ORDER[@]}" -gt 0 ] || die "LADDY_TARGETS is empty"
}
