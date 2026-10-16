# Handoff — Auto screen `m` manual pick (built, committed, pushed)
Date: 2026-10-16 11:26  ·  Project: CC-JS_claude-swap  ·  Plan: docs/plans/2026-10-15-auto-screen-manual-pick.md
Previous handoff: 2026-09-06-1727-warmup-countdown-panel-stagger-fixes.md

## Firm decisions this session
- `m` added beside `n` (n unchanged) — Jeremy's ask: pick any account, switch to it, stay until threshold.
- Engine performs the pick via `request_switch(target=N)` on the existing manual path — rejected a TUI-side `switch_to` (skips freshen/quarantine/state record).
- "Stay until threshold" = session-only engine pin that only suppresses consume-first's below-threshold nudge (`best` never moves below threshold anyway); never blocks at-limit/failover; not set in dry-run or for API-key targets; carried across `s`/`l` rebuilds; never persisted. (agent call, logged in docs/decisions.md; Jeremy has not objected)
- `m` refuses an over-threshold target even in the two states where `n` would land there (active usage unreadable / whole fleet over) — agent call, surfaced to Jeremy as changeable; no answer yet.

## Where it started
Jeremy, mid-session in a live `cswap` auto screen at 92% (threshold 98%), asked for an `m` key to manually pick an account, and whether editing would disturb his running cswap. Answered: no — modules are loaded at start (verified no lazy imports of edited modules in `tui/`); restart to get `m`, screen reopens in DRY-RUN, `l` to go live.

## What shipped
- `2292e6c` feat(tui): `m` manual pick — `src/claude_swap/autoswitch.py` (target handoff under `_switch_lock`, `_target_refusal`/`_target_usage_refusal`, `target-busy`, pin + `pinned` prop/ctor kwarg, `SwitchEvent.note`), `src/claude_swap/tui/modals.py` (`PickRow`, `AccountPickModal`), `src/claude_swap/tui/autoview.py` (binding, picker wiring, ` · holding #N` summary, pin carry-over), tests in `tests/test_autoswitch.py` (`TestManualPick`) and `tests/test_tui.py`.
- `eac1e68` docs: plan + decision entry — `docs/plans/2026-10-15-auto-screen-manual-pick.md`, `docs/decisions.md`.
- Both pushed to `origin/jeremy`.

## Dead-ends (do not retry)
- PowerShell test wrapper with a `[string[]]` remaining-args param swallows pytest's `-p`; inline `$PID` in bash breaks on backtick escaping — use `.tmp/run_tests.ps1` via `powershell -File`.

## Key files for next session
- Plan: `docs/plans/2026-10-15-auto-screen-manual-pick.md` — read this FIRST (not duplicated here)
- `docs/decisions.md` — last entry (2026-10-15) has the rejected alternatives.
- `.tmp/run_tests.ps1` — BELOW_NORMAL pytest runner (gitignored); `.tmp/test_phase2_probe.py` is an unused leftover.
- Memory touched: none

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `jeremy`, clean apart from this handoff)

## Verification — how to confirm things still work
- `.tmp/run_tests.ps1` (or `python -m pytest -q -n 4` at BELOW_NORMAL) — 2331 passed, 78 skipped.
- `python -c "import claude_swap.tui.autoview as a; print([b.key for b in a.AutoScreen.BINDINGS])"` — includes `m`.
- `cswap status` — exit 0.

## Deferred + open questions
- Open: should `m` land on an over-threshold account when `n` would (active usage unreadable / fleet all over)? — currently refuses; one-function change in `_target_usage_refusal`.
- Open: allow `m` onto disabled accounts? — currently `target-unavailable` (not in `switchable_account_numbers()`), though `cswap switch` allows it.
- Deferred (review nit, not fixed): `_restart_engine` reads `pinned` before `stop()`; a manual `_perform` racing an `s`/`l` press within one tick can drop the pin. Narrow window.
- Not yet done: Jeremy's live trial of `m` after restarting cswap.

## Pick up here
Ask Jeremy how `m` behaved after his restart, then settle the two open questions if he has a preference.
