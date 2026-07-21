"""Hub-main tripwire (spec S5, audit M3): deletion / rewind / divergence.

The VPS never writes main; merge-verified aborts the whole run when the hub's
main is not exactly where only the Director could have left it. Three tamper
shapes must each be distinguishable from the one benign case (a genuinely
fresh hub that never had a main):

  DELETED  - the hub HAD a main and now has none. Looking at the hub alone
             this is indistinguishable from a fresh hub; the difference is
             local memory of having seen one.
  REWOUND  - hub main force-pushed to an older (or sideways) commit that is
             still an ancestor of local main, so a bare ancestor check passes.
  DIVERGED - hub main carries a commit local main never merged.

Derive, don't store: the memory is git's own remote-tracking ref
(``refs/remotes/<remote>/<base>``) - the exact ref an ordinary fetch/push
already maintains - never a new state file that could drift. This module
never fetches (the old pre-check ``fetch --prune`` DELETED that evidence
before the check could read it, which is precisely defect M3a): it reads the
hub's live tip with ``ls-remote``, compares, and only on a PASS advances the
tracking ref to the verified sha. The record therefore never runs ahead of
what was checked, and a trip keeps tripping on re-runs instead of erasing its
own alarm. If the record is missing (a fresh clone), the check degrades to
the plain ancestor-of-local guarantee: the delete/rewind halves need a
last-seen sha, and a repo that never saw one has no evidence to compare -
the per-branch gates (defense in depth) still hold either way.

Leaf module: no orchestrator imports, so ``local_merge`` re-exports it the
same way it re-exports ``merge_subject``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "HubMainCheck",
    "HubMainState",
    "check_hub_main",
    "hub_main_ancestor_of_local",
]


class HubMainState(str, Enum):
    """Where the hub's main sits, relative to local main and the last-seen sha."""

    OK = "ok"  # present, fast-forward of last-seen, ancestor of local main
    FRESH = "fresh"  # hub has no main and none was ever seen - benign
    DELETED = "deleted"  # hub main was seen before and is now gone - alarm
    REWOUND = "rewound"  # hub main left the fast-forward path - alarm
    DIVERGED = "diverged"  # hub main is not an ancestor of local main - alarm
    UNREACHABLE = "unreachable"  # hub cannot be consulted (caller decides)


@dataclass(frozen=True)
class HubMainCheck:
    """Typed tripwire outcome; ``detail`` names the shas for the Director."""

    state: HubMainState
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in (HubMainState.OK, HubMainState.FRESH)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_hub_main(
    repo: Path, branch_remote: str, base_branch: str = "main"
) -> HubMainCheck:
    """Compare the hub's live ``base_branch`` tip against local main and the
    last-seen sha (the remote-tracking ref). Never fetches; advances the
    tracking ref only on a PASS (see the module docstring)."""
    tracking = f"refs/remotes/{branch_remote}/{base_branch}"
    code, out = _git(repo, "rev-parse", "--verify", "--quiet", tracking)
    last_seen = out if code == 0 else ""

    # ls-remote --exit-code: 0 = ref exists, 2 = reachable but no such ref,
    # anything else = the remote itself could not be consulted.
    code, out = _git(
        repo, "ls-remote", "--exit-code", branch_remote, f"refs/heads/{base_branch}"
    )
    if code == 2:
        if last_seen:
            return HubMainCheck(
                HubMainState.DELETED,
                f"hub {base_branch} was last seen at {last_seen} and is now "
                "GONE from the hub; a fresh hub never had one",
            )
        return HubMainCheck(
            HubMainState.FRESH,
            f"hub has never had a {base_branch}; nothing to compare against",
        )
    if code != 0:
        return HubMainCheck(HubMainState.UNREACHABLE, out)
    fields = out.split()
    if not fields:
        return HubMainCheck(
            HubMainState.UNREACHABLE, "unexpected empty ls-remote output"
        )
    hub_sha = fields[0]

    # A tip local history never contained cannot be an ancestor of local main,
    # so it classifies as DIVERGED without ever fetching the hub's objects.
    known, _ = _git(repo, "cat-file", "-e", f"{hub_sha}^{{commit}}")
    if known != 0:
        return HubMainCheck(
            HubMainState.DIVERGED,
            f"hub {base_branch} points at {hub_sha}, a commit unknown to "
            "local history",
        )
    ancestor, _ = _git(repo, "merge-base", "--is-ancestor", hub_sha, base_branch)
    if ancestor != 0:
        return HubMainCheck(
            HubMainState.DIVERGED,
            f"hub {base_branch} at {hub_sha} is not an ancestor of local "
            f"{base_branch}",
        )
    if last_seen:
        # Hub main may only ever move FORWARD (the Director pushing local
        # main); any other move - an older commit, or sideways onto some
        # already-merged commit - is a force-push where only the Director may
        # write (M3b). Equal shas pass: a commit is its own ancestor.
        forward, _ = _git(repo, "merge-base", "--is-ancestor", last_seen, hub_sha)
        if forward != 0:
            return HubMainCheck(
                HubMainState.REWOUND,
                f"hub {base_branch} moved from last-seen {last_seen} to "
                f"{hub_sha}, which is NOT a fast-forward of it (an older or "
                "sideways commit)",
            )
    if hub_sha != last_seen:
        # Record the VERIFIED tip in git's own remote-tracking ref - this is
        # what arms the delete/rewind halves for the next run. Failing to
        # record would silently weaken that run's check, so it is loud.
        code, out = _git(repo, "update-ref", tracking, hub_sha)
        if code != 0:
            raise RuntimeError(
                f"could not record verified hub {base_branch} at {tracking}: {out}"
            )
    return HubMainCheck(HubMainState.OK)


def hub_main_ancestor_of_local(
    repo: Path, branch_remote: str, base_branch: str = "main"
) -> bool:
    """Back-compat boolean over check_hub_main: True = safe to proceed (hub
    main verified in place, or a genuinely fresh hub). False = deletion,
    rewind, or divergence - the caller must abort the entire run; use
    check_hub_main directly to tell the cases apart."""
    return check_hub_main(repo, branch_remote, base_branch).ok
