# Handoff — cswap pin control: unpin, switch-source logging, released pick returns at once
Date: 2026-11-08 11:58  ·  Project: CC-JS_claude-swap  ·  Plan: docs/plans/2026-11-08-pin-control.md
Previous handoff: 2026-11-08-0044-cswap-fleet-remote-hosts.md
Session: e731535b-b8d1-4ae3-b227-3f63d18904dd

## Firm decisions this session
- Remote dashboard shows Pinned vs Auto and can release the hold. The dashboard half was built by the CC-JS_Claude_Remote session under a live peer charter (Jeremy chose it over a request file). Charter closed 2026-11-08.
- Jeremy reviewed the pin-marker UI options in the Remote session before it built one (his condition on the charter).
- TUI `u` key added (Jeremy: yes), same `release_pin` path as `cswap engine unpin`.
- Switch-source logging lives only in the engine. Plain `cswap switch` and the TUI accounts screen get no source line (upstream code; rebase cost). No source line = the switch came from outside the auto engine.
- After a released targeted pick (`m` / engine pick), the engine may go straight back to the account the pick left. Jeremy: "The behavior I want is for it to go to 4." This disarms both the anti-undo bar and the 300 s cooldown. Jeremy: "I want it to move right away." `n` keeps both, since nothing protects it from an immediate undo. Fleet-follow never counts as a pick.
- Accepted side effects (docs/decisions.md 2026-11-08): a restart while holding a pick counts as a release; a hand switch away and back onto the pick doesn't re-arm the bar; after a pick, an outside switch or full engine relaunch within 300 s can be undone at once; a second engine on the same store wouldn't respect a pick (none today).

## Where it started
Jeremy asked why auto wasn't switching to #4. Answer: a manual pick of #2 at 00:41 CT was pinned (held until the 95% threshold), and nothing showed or released it. The log couldn't tell a TUI `m` from a Remote hub pick. He asked for pin visibility and unpin on the dashboard, plus source logging.

## What shipped
- `release_pin`, logged pin helpers, switch-source and pin lines, and the `source=` argument on `request_switch` — `src/claude_swap/autoswitch.py` (110c324)
- Control op `unpin` and `cswap engine unpin [--json]` (released/not-pinned exit 0, busy exit 1 during a pick or strategy change, releases a rebuilt engine too) — `src/claude_swap/engine_control.py` (110c324)
- TUI `u` — `src/claude_swap/tui/autoview.py`; follow-switch pin clear goes through the logged helper — `src/claude_swap/fleet_leader.py` (110c324)
- `lastSwitchPick` state key; the anti-undo bar and `_in_cooldown` skip while it is set — `src/claude_swap/autoswitch.py` (6467935)
- Tests — `tests/test_autoswitch_pin_control.py` (new, fork-only), `tests/test_engine_control.py`, `tests/test_tui.py`, `tests/test_fleet_follow.py`. Suite: 3640 passed, 121 skipped.
- Docs — `docs/decisions.md` (3 entries), `docs/collaborations/2026-11-08-cswap-pin-control.md` (closed), plan marked shipped.
- Remote side (their repo, their commits): POST /api/cswap/unpin e9b4522, pin marker + tap-to-Unpin 6551c97.
- Verified live: `m` #4->#3 at 11:57:26, dashboard Unpin at 11:57:42, `engine moved #3 -> #4 (trigger consume-first, source engine)` 0.14 s later.

## Dead-ends (do not retry)
- Using `leftTrigger` as the pick signal — it reads "manual" for `n`, `m`, engine pick and fleet-follow alike.
- Disarming the bar for `n` too — the first consume-first tick would undo every `n`.
- The first live test right after a TUI restart — the restart drops the session-only pin, so there was nothing to unpin. Pin something first.

## Key files for next session
- Plan: `docs/plans/2026-11-08-pin-control.md` — read this FIRST (not duplicated here)
- `docs/decisions.md` — the three 2026-11-08 entries hold the accepted side effects
- `docs/collaborations/2026-11-08-cswap-pin-control.md` — charter and the log of both sides' work
- Memory touched: none

## Running state
- Background processes: none
- Dev servers / ports: none. Jeremy's cswap TUI is live (pid 49264, running the final code).
- Worktrees / branches: none (`jeremy`, pushed, clean)
- Scratch finished: `.tmp/2026-11-pin-control/`, `.tmp/2026-11-pin-control-deep/`

## Verification — how to confirm things still work
- `python tools/run_tests_low.py -q -n 4 -p no:cacheprovider` — 3640 passed, 121 skipped
- `cswap engine status` — running; shows `holding #N` only after an `m` / engine pick
- `grep -n "auto-switch" ~/.claude-swap-backup/claude-swap.log | tail` — source and pin lines (read-only; no emails in them)

## Deferred + open questions
- Deferred: a fleet-follow switch has no full-harness test that `lastSwitchPick` stays False (only a state assert in `tests/test_fleet_follow.py`).
- Deferred: an unpin racing a TUI `s`/`l` rebuild is narrowed, not closed (the TUI doesn't take the server lock). It self-heals on the next unpin or at the threshold.
- Deferred: picking an API-key account writes `lastSwitchPick` True without a pin. Harmless today.
- Open: none.

## Pick up here
Nothing pending on this work. The next session starts from Jeremy's next "I want cswap to…".
