# Plan — Auto screen `n` = Switch now

Date: 2026-09-04 · Branch: `jeremy` · Status: **built**
Depends on: `2026-09-04-autoswitch-fable-consume-first.md` (shipped).

## Ask
On the TUI auto screen, press `n` to switch immediately to the "Next best"
account under the currently selected strategy (persisted or session `s`
override). Footer shows `n Switch now`.

## Findings (verified in `src/claude_swap/` at jeremy@a8f2069)
- The engine already owns every safe step of a switch: poll → health checks →
  rank (`_rank_candidates`) → freshen target (token refresh, quarantine on
  `invalid_grant` / identity conflict) → `_perform` under the state lock,
  recording `lastSwitchAt/From/To`, `leftHeadroom`, `leftRecoveryAt`,
  `leftTrigger`. The dashboard's `do_switch` (`switcher.switch_to`) bypasses
  all of that; `switcher.switch(strategy=…)` knows only `best`/`next-available`.
- Trigger vocabulary today: `proactive | at-limit | failover | consume-first`.
  Anti-flap gates (cooldown, hysteresis, strictly-sooner reset, no-return bar)
  are scoped to `proactive`/`consume-first`; escapes skip them and rank by
  headroom. `all_above` (whole fleet ≥ threshold) switches the key to
  binding-recovery time for the proactive triggers.
- `wake()` cuts the inter-tick sleep; `apply_threshold()` is the precedent for
  a thread-safe session override from the TUI. `_FakeEngine` in
  `tests/test_tui.py` stands in for the engine on the TUI side.
- The "Next best" panel (`_candidates_text`) shows the strategy's ranking with
  a health tier and a `skip` tag for rows the proactive gates would refuse.

## Decision — engine-side one-shot trigger, not a TUI-side switch_to
`n` asks the ENGINE to switch on its next tick with a new trigger `"manual"`:

- `AutoSwitchEngine.request_switch()` sets a `threading.Event` and wakes the
  loop. `_tick_inner` consumes it once (clear at the top of the tick, like
  `_wake`) and, once the active account is known and not an API-key account,
  forces `trigger = "manual"` regardless of utilization.
- `manual` ranks EXACTLY like the strategy's proactive path (`best` → most
  headroom; `consume-first` → `consume_first_key`, incl. the 5h-hot tier;
  `all_above` → recovery key) but drops the gates a human has overridden:
  cooldown, hysteresis margin, strictly-sooner reset, and the no-return bar.
  It KEEPS the landing-health gate (candidate must be below the threshold
  unless `all_above`) — landing on an unhealthy account would re-trigger on
  the very next tick. Result: `n` takes the top un-skipped row of "Next best"
  in the common case; with every row skipped for margin reasons it still
  takes the top row; with every row unhealthy it emits `no-qualifying-candidate`.
- Fresh data before acting: reuse the consume-first phase-2 refetch for
  `manual` (a switch is imminent) but do not apply the `stale-usage` hold —
  the user said now; stored entries are acceptable.
- `_perform` records the switch like any other (`leftTrigger="manual"`), so
  the ordinary cooldown then protects the landing from an immediate
  proactive flap-back. `_no_return_account` / `_left_account_recovered`
  treat a `manual` record like a proactive one (numeric baseline present).
- Dry-run: `_perform`'s existing dry-run branch emits
  `[dry-run] would switch A -> B (manual)`. So `n` in DRY-RUN is a preview,
  in LIVE it switches. No confirm modal (the `l` confirm already gated live).
- TUI: `Binding("n", "switch_now", "Switch now")`; `action_switch_now` is
  inert in threshold-adjust mode (same as `s`), otherwise calls
  `engine.request_switch()` and writes a muted log line
  `— switch now requested (<strategy>) —`. `_FakeEngine` gains
  `request_switch()` + a counter.

## Blast radius
Engine decision path only. No credential read/write change, no store layout,
no slot-copy step → no `deep` hop. `review` (Opus 5) before commit.

## Work items
- A. `src/claude_swap/autoswitch.py`: `request_switch`, tick consumption,
  `manual` in the trigger sets (`_no_return_account` scoping stays
  proactive/consume-first only — manual never bars), `_rank_candidates`
  manual gates, phase-2 refetch, `SwitchEvent.trigger` comment/docstrings.
- B. `tests/test_autoswitch.py`: manual under `best` below threshold picks the
  most-headroom candidate and ignores hysteresis; manual under `consume-first`
  ignores strictly-sooner and cooldown, respects the 5h-hot tier order; manual
  never lands on an at/over-threshold candidate; manual is consumed once (the
  following tick is a normal poll); dry-run manual emits a dry-run switch and
  writes nothing; manual records `leftTrigger="manual"` and the next proactive
  tick is in cooldown.
- C. `src/claude_swap/tui/autoview.py` + `tests/test_tui.py`: binding, action,
  log line, inert during adjust mode, `_FakeEngine.request_switch`.
- D. Full suite at BELOW_NORMAL `-n 4`; `cswap status` smoke; Jeremy relaunches
  the TUI and tries `n` in dry-run first.

## Upstream
Generic feature → shaped for an upstream PR (`feat/tui-switch-now`) on top of
the Next-best branch; cut when Jeremy says.

## Status log
- 2026-09-04 — planned; building via tool-runner.
- 2026-09-04 (later) — built. Engine + tests committed, TUI `n` committed. Review
  (Opus 5) found two defects in the unreadable-active path (landing gate
  refused what failover would take; ordinary recovery legs released the
  no-return bar on a null baseline) — both fixed, mutation-checked, covered.
  Full suite 2132 passed / 77 skipped. Next: Jeremy relaunches the TUI and
  tries `n` in dry-run; upstream PR `feat/tui-switch-now` deferred.
