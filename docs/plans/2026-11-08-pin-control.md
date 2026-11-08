# Plan: pin visibility, unpin, and switch-source logging

Date: 2026-11-08 · Branch: `jeremy` · Status: **shipped + verified live** (110c324, 6467935; 2026-11-08). Adds beyond the original plan: a released m/engine pick skips the anti-undo bar and the cooldown (`lastSwitchPick`, decisions 2026-11-08)
Collaboration: `docs/collaborations/2026-11-08-cswap-pin-control.md` (Claude Remote shows the pin and calls unpin).
Shape: fork-only. Additions to `engine_control.py` (fork module), a small engine
method in `autoswitch.py`, one TUI key, and fork-marked log lines. No upstream PR.

## Why
2026-11-08: the auto engine sat on #2 for hours while consume-first wanted #4.
The cause was a manual pick at 00:41 CT, held (`pinned`) until the 95% threshold.
Nothing on the Remote dashboard showed the hold, there was no way to release it
short of switching, and the log line ("Switched from account 3 to 2") did not
say who switched (the TUI's `m`, or the hub's `cswap engine pick`).

## The change
1. **Release the hold: `cswap engine unpin [--json]`** (new control op `unpin`).
   - The engine gets `release_pin(source)`: it clears `_pinned` (safe against a
     concurrent tick) and wakes the engine, so the next tick runs the normal
     strategy (consume-first may then move off the account at once).
   - The reply comes back immediately and does not wait for the resulting switch.
     The caller re-reads `status` to see what happened.
   - Replies (contract string stays `cswap-engine/0.1`; additive amendment):
     - `{"op": "unpin", "result": "released", "was": "2"}`, exit 0
     - `{"op": "unpin", "result": "not-pinned"}`, exit 0 (idempotent; also a fleet worker, which never pins)
     - No engine: the same `no-engine` shape as the other ops, exit 3
     - A pick in flight: `result: "error"`, `error: "busy"`, exit 1 (the pick would re-pin when it lands)
   - An engine built before this answers `bad-request` (exit 1). The hub treats that as "update cswap".
2. **TUI `u` key** on the auto screen: the same `release_pin`, with a one-line
   status message ("released hold on #2" / "no hold to release").
3. **Show the pin.** `status` already reports `engine.pinned`. No change on this side.
   The dashboard badge belongs to Remote (see the collaboration).
4. **Switch-source logging.** Fork-marked INFO lines in `claude-swap.log`:
   - One line right after each engine switch, carrying the trigger
     (`consume-first`, `priority`, `proactive`, `at-limit`, `failover`,
     `manual`, `fleet-follow`) and the source (`tui-n`, `tui-m`,
     `engine-pick` = `cswap engine pick` / Remote hub, `engine` = autonomous,
     `fleet-lease`).
   - Pin lifecycle lines: pin set (by whom), released (`tui-u`,
     `engine-unpin`, threshold reached, moved off the pick, or follow).
   - Non-engine switches (plain `cswap switch` CLI, TUI accounts screen) get a
     source line at their entry point if cheap; otherwise documented as
     "no source line = outside the auto engine".
   - **Constraint:** the new lines must NOT match `Switched from account (\d+) to (\d+)`
     (`menubar.py` parses that line into switch history). Upstream's own line
     stays as it is.
   - No emails anywhere in these lines. Accounts are numbers only.

## Blast radius
Engine tick and `_perform` gain logging and a pin-release path. No credential,
store-layout or slot-copy change. Because it sits on the switch path, `review`
reviews it and `deep` verifies it adversarially (race: unpin vs. an in-flight `m`
or engine pick; the TUI's engine rebuild carrying `pinned=`; the fleet leader's
pin; log lines leaking emails or matching the menubar regex).

## Tests
- `tests/test_engine_control.py`: unpin released / not-pinned / no-engine / busy /
  bad-request on an old engine; exit codes; human line.
- `tests/test_autoswitch*.py`: `release_pin` clears the hold, wakes the engine,
  and the next tick runs consume-first (switches to a sooner-resetting account);
  a release racing a manual pick ends with the pick's pin (latest action wins).
- Log tests (caplog): each trigger/source combination emits its source line;
  no new line matches `menubar._SWITCH_LOG_RE`.
- TUI: `u` binding calls `release_pin` (existing autoview test style).
- Suite at BELOW_NORMAL: `python tools/run_tests_low.py -q -n 4 -p no:cacheprovider`.

## Live verification (needs Jeremy's OK, his engine is live)
`cswap engine status --json` shows `pinned`, then `cswap engine unpin --json`,
then status shows `pinned: null` and the engine's next move follows consume-first.
The running TUI must be restarted to load the new code (Jeremy's step).

## Rebase cost
`engine_control.py` is fork-only. In `autoswitch.py`: one method, the
log calls at the trigger sites, fork-marked. In `autoview.py`: one binding + action.
