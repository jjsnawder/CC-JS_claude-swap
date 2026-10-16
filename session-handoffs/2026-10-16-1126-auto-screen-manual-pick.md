# Handoff — Auto screen `m` manual pick (built, committed, pushed)
Date: 2026-10-16 11:26  ·  Project: CC-JS_claude-swap  ·  Plan: docs/plans/2026-10-15-auto-screen-manual-pick.md
Previous handoff: 2026-09-06-1727-warmup-countdown-panel-stagger-fixes.md

## Firm decisions this session
- `m` added beside `n` (n unchanged) — Jeremy's ask: pick any account, switch to it, stay until threshold.
- Engine performs the pick via `request_switch(target=N)` on the existing manual path — rejected a TUI-side `switch_to` (skips freshen/quarantine/state record).
- "Stay until threshold" = session-only engine pin that only suppresses consume-first's below-threshold nudge (`best` never moves below threshold anyway); never blocks at-limit/failover; not set in dry-run or for API-key targets; carried across `s`/`l` rebuilds; never persisted. (agent call, logged in docs/decisions.md; Jeremy has not objected)
- [Jeremy, 2026-10-16] `m` follows `n`'s landing rule: `_target_usage_refusal` runs `n`'s manual ranking, so `m` lands over threshold exactly where `n` does (active unreadable / whole fleet over). Replaces the shipped always-refuse rule.
- [Jeremy, 2026-10-16] `m` may pick a DISABLED account (managed + usable login), added to that tick's candidates only; auto never selects it (verified: `switchable_account_numbers()` filters disabled, `src/claude_swap/switcher.py:1837`).
- [Jeremy, 2026-10-16] "Next best" panel may keep listing disabled accounts — fine, as long as auto never picks them. Not a bug to fix.

## Where it started
Jeremy, mid-session in a live `cswap` auto screen at 92% (threshold 98%), asked for an `m` key to manually pick an account, and whether editing would disturb his running cswap. Answered: no — modules are loaded at start (verified no lazy imports of edited modules in `tui/`); restart to get `m`, screen reopens in DRY-RUN, `l` to go live.

## What shipped
- `2292e6c` feat(tui): `m` manual pick — `src/claude_swap/autoswitch.py` (target handoff under `_switch_lock`, `_target_refusal`/`_target_usage_refusal`, `target-busy`, pin + `pinned` prop/ctor kwarg, `SwitchEvent.note`), `src/claude_swap/tui/modals.py` (`PickRow`, `AccountPickModal`), `src/claude_swap/tui/autoview.py` (binding, picker wiring, ` · holding #N` summary, pin carry-over), tests in `tests/test_autoswitch.py` (`TestManualPick`) and `tests/test_tui.py`.
- `eac1e68` docs: plan + decision entry — `docs/plans/2026-10-15-auto-screen-manual-pick.md`, `docs/decisions.md`.
- `fa755c9` feat(autoswitch): `m` follows `n`'s landing rule and may pick a disabled account — `src/claude_swap/autoswitch.py`, `src/claude_swap/tui/autoview.py`, `src/claude_swap/tui/modals.py`, tests. Reviewed: nothing material.
- `7ac7f04` docs: rulings — plan updated, `docs/decisions.md` 2026-10-16 entry.
- All pushed to `origin/jeremy`.

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
- `.tmp/run_tests.ps1` (or `python -m pytest -q -n 4` at BELOW_NORMAL) — 2343 passed, 78 skipped.
- `python -c "import claude_swap.tui.autoview as a; print([b.key for b in a.AutoScreen.BINDINGS])"` — includes `m`.
- `cswap status` — exit 0.

## Deferred + open questions
- Both open questions resolved by Jeremy 2026-10-16 (see Firm decisions); shipped in `fa755c9`.
- Known, deliberate: `m` differs from `n` only in (a) taking an included API-key target under consume-first, (b) always deciding on the fresh second fetch.
- Deferred (review nit, not fixed): `_restart_engine` reads `pinned` before `stop()`; a manual `_perform` racing an `s`/`l` press within one tick can drop the pin. Narrow window.
- Not yet done: Jeremy's live trial of `m` after restarting cswap.

## Pick up here
Feature complete. Ask Jeremy how `m` behaved in live use after restarting cswap; fix only what he reports.
