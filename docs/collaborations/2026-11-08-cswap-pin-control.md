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
- 2026-11-08 09:25 CT · sent charter request to the Remote session ("charter with swap") with charter path @ 56ecca7; awaiting its approval
- 2026-11-08 09:26 CT · Remote approved its side as written (Jeremy, in its session); its log @ fd9062b. It starts the UI mockups + Unpin route vs fakes; wants a message when unpin is committed and installed
- 2026-11-08 09:57 CT · unpin + u + source logging committed 110c324 (review: safe to commit; deep: ship; fixes applied, suite 3630 passed/121 skipped); decisions 6bb2b3c. Told Remote: installed, busy also on strategy change, running TUI pid 16928 answers bad-request until restart; live status active 4 pinned 4
- 2026-11-08 10:03 CT · Remote shipped: hub route POST /api/cswap/unpin (e9b4522), pin marker + tap-to-Unpin UI (6551c97), hub restarted 10:03 CT; read-only shows pinned 4. Waiting on Jeremy: TUI restart, then his dashboard Unpin; I check the source line in claude-swap.log after
- 2026-11-08 10:35 CT · live test (Jeremy): TUI restarted (pid 49000, restart drops the pin); m #4->#3 then dashboard Unpin. Log: "engine moved #4 -> #3 (trigger manual, source tui-m)", "holding #3 (pin set by tui-m)", "hold on #3 released (engine-unpin)"; status pinned null. Stayed on #3: anti-undo bar barred #4. Jeremy: "The behavior I want is for it to go to 4." Follow-up in progress: bar disarmed after a manual switch
