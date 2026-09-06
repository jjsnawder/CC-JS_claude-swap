# Handoff — Warmup countdown panel shipped; stagger tolerance + next-up priority fixed; first live hello observed
Date: 2026-09-06 17:27  ·  Project: CC-JS_claude-swap  ·  Plan: `docs/plans/2026-09-06-warmup-stagger.md`
Previous handoff: `2026-09-06-1610-warmup-live-trial.md`

## Firm decisions this session
- Warmup hellos are isolated from Jeremy's other sessions (question answered, verified in code): each hello is a `claude -p` child with `CLAUDE_CONFIG_DIR` set to the account's slot dir (store→slot one-way credential copy); it never writes the machine default login. The active account is hello'd via the default config home. Only the auto-SWITCH (live mode) changes the default login — not warmup.
- Countdown panel reads an engine-owned schedule mirror (`build_schedule` → frozen `WarmupSlot` rows, `engine.warmup_schedule()`), never the state file and never a second planner in the TUI. Cleared only when the warmup step ran, found itself disabled, and no hello is in flight. `(stale)` marker after 2× poll interval.
- Stagger tolerance is EARLY-side only: `min(interval_seconds + 60 s, spacing/2)`; the late side keeps `spacing/2` (an unbounded late side puts two windows on one phase with 3 cold accounts — measured, pinned by test). Rejected: the original symmetric `spacing/2`, which fired the first live hello 37 min early.
- Warmup priority = the candidates panel's ranking: planner sorts its target instants and assigns them in caller order; engine passes cold accounts ordered by `_rank_candidates(trigger="manual")` for the configured strategy (margins waived, landing-health gate kept), then the active account, then the just-switched-to one; ties/unranked by slot number; ranking failure → slot order, debug log only. Rejected: slot order (built behaviour), `trigger="proactive"` (hysteresis margin collapses the order to slot order under `best`), a reimplemented ranking.
- Neither stagger fix was folded into the panel commit — separate concern, separate commits.

## Where it started
Resumed from the 16:10 handoff (flag on, first hello pending). Jeremy asked (1) whether warmups hijack his other VS Code sessions — no; (2) for an overtly visible per-account countdown to the next warmup on the live auto screen — built; (3) why Account-3 was warmed before next-up Account-1 — planner walked slot order and tolerance was half a spacing; he approved both stagger fixes.

## What shipped
- `04e6539` feat(warmup): `WarmupSlot`, `build_schedule`, `IN_FLIGHT_REASON`, `slot_sort_key`; engine `_warm_schedule`/`_warm_ran`/`_warm_off`, `warmup_schedule()`, `warmup_enabled` — `src/claude_swap/warmup.py`, `src/claude_swap/autoswitch.py`, `tests/test_autoswitch.py`
- `2d743c2` feat(tui): `#warmup-panel` under `#candidates`, `warmup_panel_text`, `countdown_text`, 1 s timer with no-op repaint skip, `max-height: 8` — `src/claude_swap/tui/autoview.py`, `src/claude_swap/tui/cswap.tcss`, `tests/test_tui.py` (`_FakeEngine` gained `warmup_schedule()`, `warmup_enabled`)
- `325e072` fix(warmup): `plan_warmups(tolerance_s=)`, `DEFAULT_TOLERANCE_MARGIN_S`, sort-then-assign; engine `_warmup_order(...)`, `_warm_plan` now a 6-tuple (now, candidates, stagger, current, usage, headroom) — same four src/tests files + `tests/test_warmup.py`
- `b8d5878`, `10d1015` docs: four `docs/decisions.md` entries; plan status/rules revised
- All pushed to `origin/jeremy`; HEAD `10d1015`; tree clean. Suite: 2295 passed / 78 skipped (was 2261 at session start).
- Operator config unchanged; live store untouched except the sanctioned `cswap status` smoke tests.

## Dead-ends (do not retry)
- Unbounded late-side tolerance ("past target → ping now, any amount") — coincident phases with 3 cold accounts; reverted to `spacing/2` late bound.
- `_rank_candidates(trigger="proactive")` for warmup ordering — hysteresis margin drops cold candidates that don't beat the active account; use `"manual"`.
- `test_three_strikes_buy_a_day_off` flaked once in ~13 full runs (pre-existing thread-timing flake, not reproduced); don't chase it as a regression from this work.

## Key files for next session
- Plan: `docs/plans/2026-09-06-warmup-stagger.md` — read this FIRST (status log has today's entries)
- `docs/decisions.md` — last four entries (panel mirror; live-hello findings; tolerance; priority)
- `src/claude_swap/warmup.py` `plan_warmups` / `build_schedule` — if observed hello times disagree with the panel
- `src/claude_swap/autoswitch.py` `_spawn_planned_warmups` / `_warmup_order` — engine call site
- `~/.claude-swap-backup/autoswitch_state.json` `warmup.<num>` — hello log per slot (read-only)
- Memory touched: none

## Running state
- Background processes: none (previous session's Monitor `bjmaqwb17` and Bash `btq3oim9y` both completed this session when the first hello landed — nothing to kill; the tool-runner/review subagents are finished)
- Dev servers / ports: none
- Worktrees / branches: `jeremy` clean at `10d1015`. Jeremy's live TUI `cswap.exe` PID 6220 (started 16:05) is STILL ON THE OLD CODE — needs a relaunch to get the panel and the new stagger rules. Never kill by name.

## Verification — how to confirm things still work
- Relaunch cswap → auto screen: a `WARMUP · next: Account-N in H:MM:SS` block under the candidates list, one row per account, ticking every second; `(stale)` never shows in normal operation
- `python -m pytest -q -n 4` (BelowNormal) — 2295 passed / 78 skipped
- `cswap status` — Account-4 active, 5h resets 20:59/21:00
- `python -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude-swap-backup/autoswitch_state.json'))).get('warmup'))"` — `{'3': {lastPingAt≈16:38:17, model haiku, ok, failures 0}}` plus new entries as Accounts 1/2 get hello'd
- Observed: first live hello Account-3 16:38:17 ok (reset 21:38). Under the NEW rules, after relaunch, Accounts 1/2 take the remaining gaps around phases 21:00/21:38, next-up first; panel shows exact times.

## Deferred + open questions
- Open (Jeremy): confirm the 16:38 hello produced no window flash and the TUI terminal stayed clean.
- Deferred (carried): `n` live trial; upstream PRs incl. `feat/warmup-stagger` (now also carries the panel + tuning); `workflows/reconcile-upstream.md` / `verify-change.md`; retiring the two Task Scheduler warmup jobs once this behaves for a day.
- Deferred (nits from review, not done): `countdown_text` >24 h renders without a day marker; lapsed `reset_ts` on a warm row shows a past clock time — both unreachable in practice.
- Deferred: `tools/report_model_usage.py` cost recap not run this session.

## Pick up here
Ask Jeremy whether he relaunched cswap and what the WARMUP panel shows for Accounts 1/2 (times + that the ranking puts next-up first); compare against `warmup.<num>` in the state file when the hellos land. No code change is owed unless the observed times disagree with the panel.
