---
status: done
type: spike
---
# Document the frontend `.env` + map provider toggle in README

## Goal

`README.md`'s "Local setup (one-time)" section (around line 79-104)
documents the repo-root `.env` (backend `DATABASE_URL` etc.) but never
mentions `frontend/.env` at all, even though `frontend/.env.example`
already exists and requires a real value for local map rendering to work
(`VITE_MAPBOX_TOKEN`, per `frontend/.env.example:7-8`). Someone following
"Local setup" top to bottom today gets a working backend and a frontend
that silently falls back (or breaks) on the map-dependent pages with no
pointer to why.

Add a short subsection documenting:
1. Copy `frontend/.env.example` to `frontend/.env` as part of local setup
   (fits naturally right after step 3, "Install backend + frontend deps",
   or as its own step — pick whichever reads better in context).
2. What `VITE_MAPBOX_TOKEN` is for and where to get one (a Mapbox account
   access token — the `.env.example` comment already has the URL, reuse
   it) and that the app **works without it**: the map provider toggle is
   `leaflet` (default, no token needed) vs `mapbox` (needs the token).
   The toggle is user-facing UI state stored in `localStorage`
   (`useMapPreference` in
   `frontend/src/features/maps/hooks/useMapPreference.ts`,
   `storageKeys.mapPreferred`), not an env-only switch — read that hook to
   describe it accurately, don't guess.

## Scope

**In scope:**
- Edit `README.md` only — add the subsection described above under
  "Local setup (one-time)".

**Out of scope — do not do any of this:**
- Do not change `frontend/.env.example`, `useMapPreference.ts`, or any
  other code — this is a documentation-only task.
- Do not touch any other README section.
- Do not invent details not verifiable in the code — if something about
  the toggle UI (e.g. where in the app a user changes it) isn't obvious
  from `useMapPreference.ts` or its usage sites, describe only what's
  verifiable and skip the rest rather than guessing.

## Acceptance criteria

1. `README.md` gains a short (a few sentences + the copy command), accurate
   subsection under "Local setup (one-time)" covering: copying
   `frontend/.env.example` → `frontend/.env`, what `VITE_MAPBOX_TOKEN` is
   for, where to get one, and that `leaflet` (no token) is the default
   with `mapbox` as an opt-in per-user preference.
2. No other file is changed.
3. The new text is factually consistent with
   `frontend/src/features/maps/hooks/useMapPreference.ts` and
   `frontend/.env.example` as they exist in this repo — no invented API,
   command, or config var.

## Notes for the reviewer

- This is the **first-ever real end-to-end run** of the agent dev-loop
  (`.laddy/orchestrator/`) — deliberately the smallest safe real task
  (docs-only, single file, no product code touched) per
  `docs/development/runbooks/agent-dev-loop-setup.md` §7's slice-1 smoke
  procedure. Reject on scope creep even more strictly than usual — the
  point of this run is to prove the loop converges and pushes cleanly,
  not to land a big README rewrite (`TODO.md`'s "D-2 · README update
  (jeden balík)" has more items; they are deliberately NOT in this task).
