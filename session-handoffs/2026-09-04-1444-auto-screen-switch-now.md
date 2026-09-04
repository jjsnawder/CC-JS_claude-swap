# Handoff — Auto screen `n` = Switch now (engine `manual` trigger)
Date: 2026-09-04 14:44  ·  Project: CC-JS_claude-swap  ·  Plan: `docs/plans/2026-09-04-auto-screen-switch-now.md`
Previous handoff: `2026-09-04-1344-autoswitch-fable-consume-first.md` (its work is closed out: Jeremy confirmed the TUI looked right, full suite rerun green 2116/77)

## Firm decisions this session
- `n` goes THROUGH THE ENGINE as a one-shot `request_switch()` → next tick runs with trigger `manual`. Rejected: TUI-side `switcher.switch_to` like the dashboard (skips freshen/quarantine and leaves the anti-flap state blind); adding `consume-first` to `switcher.switch(strategy=…)` (duplicates ranking outside the engine). Logged in `docs/decisions.md`.
- `manual` ranks exactly like the strategy's proactive path (best → headroom; consume-first → `consume_first_key` incl. 5h-hot tier; all-above → binding recovery) and WAIVES cooldown, hysteresis, strictly-sooner reset, and the no-return bar. KEEPS the landing-health gate (candidate below threshold) — except when the active account's usage is unreadable, where manual behaves as an escape (review fix W1).
- A manual departure from an unreadable active records `(None, None)` + `leftTrigger="manual"` and takes the FAILOVER recovery legs in `_left_account_recovered`, scoped to manual only so upstream's consume-first split-shape handling is untouched (review fix W2). Without it the engine could undo the user's own switch after cooldown.
- No confirm modal on `n`. DRY-RUN previews (`[dry-run] would switch A -> B (manual)`), LIVE switches; `l` already confirmed live. Inert in threshold-adjust mode (like `s`), hidden from the footer there via `check_action`.
- The press is consumed even if the tick errors (accepted trade-off; no re-arm on ERROR).
- Manual under consume-first with only API-key candidates emits a distinct `no-oauth-candidate` reason (NO_ACTION), not `no-comparison`.
- No `deep` hop: engine decision path only, no credential/store/slot-copy change. `review` (Opus 5) ran before commit and its two Warnings + nits were applied and mutation-tested.

## Where it started
Resume closed out the prior handoff (TUI confirmed, suite rerun, `pr-313` branch deleted). Jeremy then asked for a footer hotkey, `n` = Switch Now, on the auto screen: in live mode switch immediately to the Next best account under the selected strategy.

## What shipped
All on `jeremy`, pushed to `origin`, head 2a7d295:
- 2a2afe5 — plan + decision entry (`docs/plans/2026-09-04-auto-screen-switch-now.md`, `docs/decisions.md`)
- 1764ce4 — engine: `_switch_now` event, `request_switch()`, tick consumption, `manual` trigger, `proactive_like` refactor in `_rank_candidates`, unreadable-active escape, failover legs for the manual null snapshot, trigger-aware `no-qualifying-candidate` detail, `no-oauth-candidate` reason (`src/claude_swap/autoswitch.py`; `tests/test_autoswitch.py::TestManualSwitch`, 12 tests)
- 11d3cb4 — TUI: `Binding("n", "switch_now", "Switch now")`, `action_switch_now`, `check_action`, muted log line `— switch now requested (<strategy>) —` (`src/claude_swap/tui/autoview.py`; `tests/test_tui.py` 4 tests, `_FakeEngine.request_switch` + `switch_requests` counter)
- 2a7d295 — plan status log marked built
- Local branch `pr-313` deleted (was 8b508dc; PR #313 lives on GitHub).
- `docs/model-usage.md` (gitignored) regenerated at session end — see the recap in chat.

## Dead-ends (do not retry)
- `Start-Process -ArgumentList` does not quote: a `-k "a or b"` expression splits into three args and pytest silently collects nothing. Use a single-token `-k` or run the file.
- A bash heredoc containing `"\n"` inside Python replacement strings mangles it into a real newline — use the Write tool for test blocks with escapes.
- Test fixture gotcha: with TIED 5h/7d pcts, `_binding_recovery_ts` takes the first max window; a test needing a finite binding recovery must make the intended window strictly the max.

## Key files for next session
- Plan: `docs/plans/2026-09-04-auto-screen-switch-now.md` — read this FIRST (design + review findings)
- `docs/decisions.md` — 2026-09-04 "Auto-screen `n`" entry
- `src/claude_swap/autoswitch.py` — `request_switch` (~:2470), tick consumption (~:986), `if manual: trigger = "manual"` (~:1174), `proactive_like` + manual gates in `_rank_candidates` (~:1937-2120), `is_failover_snapshot` manual scoping (~:1783)
- `src/claude_swap/tui/autoview.py` — `action_switch_now` (~:255), `check_action` (~:167)
- `tests/test_autoswitch.py::TestManualSwitch`, `tests/test_tui.py` `-k switch_now`
- Memory touched: none

## Running state
- Background processes: none (tool-runner and review subagents completed)
- Dev servers / ports: none
- Worktrees / branches: `jeremy` clean at 2a7d295 (+ this handoff); `main` untouched at upstream. Jeremy's live TUI (cswap.exe, started 13:45) predates 1764ce4/11d3cb4 — he relaunches himself; never kill by name.

## Verification — how to confirm things still work
- `powershell -NoProfile -Command '$p = Start-Process python -ArgumentList "-m","pytest","-q","-n","4","-p","no:cacheprovider" -PassThru -NoNewWindow -RedirectStandardOutput .tmp/pytest.out; $p.PriorityClass="BelowNormal"; $p.WaitForExit(); Get-Content .tmp/pytest.out | Select-Object -Last 4'` — expect 2132 passed, 77 skipped (~60 s); known flake `TestWatchScreen::test_late_normal_can_advance_usage_after_store_repaint`
- `cswap status` — live smoke (passed 14:40; redact emails if pasting)
- TUI auto screen: footer shows `n Switch now`; in DRY-RUN `n` logs the muted request line then `[dry-run] would switch … (manual)` naming the top healthy Next best row; in LIVE it switches and the poll log shows the new active.

## Deferred + open questions
- Deferred: Jeremy has not yet tried `n` in the relaunched TUI (dry-run first, then live).
- Deferred: upstream PRs, now three — `feat/consume-first-5h-guard` (standalone), `feat/tui-next-best-follows-strategy` and `feat/tui-switch-now` (both depend on upstream #313 merging). Cut from `v0.26.0`, code commits only. Jeremy to say when.
- Deferred: `workflows/reconcile-upstream.md` and `workflows/verify-change.md` still unwritten (needs Jeremy's confirmation).
- Deferred (carried): `cswap list` headroom ignores the `models` filter (`switcher.py:215`); consume-first ranks on the 7d reset not the Fable window's; 5h-hot margin reusing `hysteresisPct`.
- Open: whether `n` should re-arm when the tick ERRORs instead of silently consuming the press.
- Open: under `best`, Next best never tags an unhealthy row `skip`, so the panel's top row can be one `n` refuses; under consume-first `n` takes margin-`skip` rows. Docstring says so; a UI tweak (tag unhealthy rows under `best`) is unmade.

## Pick up here
Ask Jeremy how `n` behaved in the relaunched TUI (dry-run preview, then live), then decide with him whether to cut the three upstream PRs or wait for #313.
