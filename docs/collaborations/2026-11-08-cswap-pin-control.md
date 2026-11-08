# Collaboration: cswap pin control (show the hold, release it, from Claude Remote)
Status: active
Projects: CC-JS_claude-swap ↔ CC-JS_Claude_Remote     This file: CC-JS_claude-swap's side (initiator)
Charter: below

## Purpose
Show on the Remote dashboard whether the cswap auto engine is holding a manual pick
(pinned) or running in auto mode, and let Jeremy release the hold from the dashboard
so the engine goes back to choosing accounts by strategy.

## In scope
- **cswap (this repo):** a new `cswap engine unpin [--json]` command (control op
  `unpin`), a TUI `u` key, and switch-source logging. Contract amendment to
  `cswap-engine/0.1` in `docs/plans/2026-11-08-pin-control.md` §The change 1:
  - `{"contract": "cswap-engine/0.1", "op": "unpin", "result": "released", "was": "2"}`, exit 0
  - `{"... "op": "unpin", "result": "not-pinned"}`, exit 0 (idempotent)
  - no engine → `result: "no-engine"`, exit 3
  - pick in flight → `result: "error"`, `error: "busy"`, exit 1
  - engine older than this change → `error: "bad-request"`, exit 1
  - The reply does not wait for the resulting switch. Re-read `status`.
- **Remote:** in the cswap panel (`static/app.js`, `static/monitor.js`), show the
  mode: "Pinned #N" (from the existing `pinned` field, already passed through
  `cswapctl.py`) vs "Auto". **Jeremy reviews UI options first:** Remote shows
  him a few ways to mark the pinned item (e.g. mockups/previews in an
  AskUserQuestion in its own session) and builds only the one he picks. Add an "Unpin" control shown only while pinned:
  a new hub route (e.g. `POST /api/cswap/unpin`) → `cswapctl` → `cswap engine
  unpin --json`, then refresh status. Tests use the fake runner only.

## Out of scope
- Any other change to the cswap pick/strategy/switch flows or the hub's fallback paths.
- Displaying the new switch-source log lines on the dashboard (log-only for now).
- Fleet/remote-host (dev1) pin control. Workers never pin.

## Write access
None. Each session writes only in its own repo. Reading the other repo is fine.

## Gated steps
- Any **live** unpin or switch against Jeremy's running engine: his OK first, in the
  session doing it. Before that point, tests run against fakes only.
- Restarting the cswap TUI to load new code is Jeremy's step.

## Done when
Remote's dashboard shows Pinned/Auto correctly from a live engine, and one
Jeremy-approved live Unpin from the dashboard clears the pin (`cswap engine status`
shows `pinned: null`), with the source line visible in `claude-swap.log`.

## Approvals
- 2026-11-08 CC-JS_claude-swap: Jeremy chose "Live peer charter", then on the charter text: "You might want to add that I'll want to review some UI options for how to designate an item as pinned." (added to In scope; taken as approval with that addition)

## Log
- 2026-11-08 · plan written (`docs/plans/2026-11-08-pin-control.md`); tool-runner building the cswap side (uncommitted)
- 2026-11-08 · charter approved with the UI-options review added; committed + pushed
