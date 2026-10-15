# Plan — Auto screen `m` = Manual pick

Date: 2026-10-15 · Branch: `jeremy` · Status: **built**
Depends on: `2026-09-04-auto-screen-switch-now.md` (shipped).

## Ask
On the TUI live auto screen, `m` opens a picker of every managed account;
the chosen account becomes active and stays active until it reaches the
threshold, then normal auto-switching resumes. `n` (Switch now) is unchanged.

## Decision — reuse the `n` engine path with a target
- `AutoSwitchEngine.request_switch(target=None)`: `None` is today's `n`. A
  target makes the manual tick's candidate list exactly `[target]`, so the
  engine still owns health checks, two-phase fresh fetch, freshening,
  quarantine and `_perform`. The screen never calls `switch_to` itself.
- Refusals (NoSwitchEvent, NO_ACTION): `already-active`,
  `target-unavailable`, `target-quarantined`, `target-over-threshold`
  (landing there re-triggers next tick), `target-usage-unknown`,
  `target-busy` (live `cswap run` session or warmup hello in flight).
  Cooldown / hysteresis / sooner-reset / no-return gates are waived. A failed
  freshen of the target never slides to another account. An API-key target
  is switched to but never pinned (no quota to hold against).
- **Hold until threshold.** `best` already never moves below threshold.
  `consume-first` nudges below threshold, so a LIVE targeted switch sets a
  session-only engine pin: while active == pinned and below threshold, the
  consume-first nudge becomes `NoSwitchEvent("pinned")`. The pin clears when
  the active account changes or reaches the threshold; escapes (at-limit,
  failover) are never blocked. Carried across `s`/`l` engine rebuilds; never
  persisted.
- Picker: modal in `tui/modals.py`, rows from the app snapshot with the
  engine's binding pct, active row disabled, over-threshold rows tagged but
  selectable (the engine explains the refusal). Arrows+enter, digits 1–9,
  escape cancels. Inert in threshold-adjust mode.

## Blast radius
Switch decision path only (`_tick_inner` manual branch). No credential
read/write changes, no store layout or slot-copy changes; restic and
cross-sessions are out of scope. `review` before commit.

## Tests
`tests/test_autoswitch.py`: targeted switch beats strategy ranking, each
refusal, dry-run preview, one-shot consumption, pin hold/clear/carry-over,
pin never blocks at-limit. `tests/test_tui.py`: `m` opens picker, pick →
`request_switch(target=…)`, digit shortcut, escape no-op, adjust-mode inert.

## Rollout
Edits don't affect a running `cswap` (modules are loaded at start). Jeremy
restarts the TUI to get `m`; the auto screen opens in DRY-RUN, `l` to go live.
