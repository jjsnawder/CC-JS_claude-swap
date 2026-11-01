# Handoff — consume-first-reserve-jj strategy + `cswap engine` control channel for Claude Remote
Date: 2026-11-01 16:28  ·  Project: CC-JS_claude-swap  ·  Plan: `docs/plans/2026-11-01-consume-first-reserve.md`, `docs/plans/2026-11-01-engine-control.md`
Previous handoff: 2026-10-16-1126-auto-screen-manual-pick.md

## Firm decisions this session
- New strategy named `consume-first-reserve-jj` (Jeremy renamed it from `consume-first-reserve`): consume-first, but the reserve accounts are consumed last.
- Reserve last, for every trigger: a reserve is a target only when no non-reserve is a healthy landing. Escapes take a below-threshold reserve before an over-threshold non-reserve. The below-threshold nudge never lands on a reserve.
- While on a reserve below the threshold, the engine leaves right away (Jeremy chose this over staying until the threshold). The sooner-reset gate is waived. Cooldown, the `m` pin and the no-return bar still apply.
- Fork-only, not shaped for an upstream PR (Jeremy).
- `autoswitch.reserveAccounts` defaults to `"1"` (Jeremy). `none`/`false` means no reserve, and `config unset` goes back to "1". A slot number is fine in the public repo; an email never is.
- Session overrides: `--reserve NUMS|none` on `cswap auto` and on the TUI launch (`cswap`, `tui`, `watch`), which also take `--strategy`.
- The Next best panel follows the engine's tiers, not a literal "reserve always last". A no-return-barred non-reserve still keeps the reserve out.
- Jeremy approved the Claude Remote collaboration (charter `cswap-remote-control`) as written. It is now closed.
- `cswap engine` contract `cswap-engine/0.1` plus amendments. Exit codes: 0 done / 1 error / 2 usage / 3 no engine / 4 pending / 5 refused. The hub falls back to plain cswap only on exit 3.
- Restic exclude for `engine-control/`: Jeremy said "Leave it".
- `consume-first-reserve-jj` is now Jeremy's SAVED default strategy, set live via the engine strategy test (`autoswitch.strategy` in his settings.json).

## Where it started
Jeremy asked for a recap of the auto options, then asked for a reserve-last strategy for account #1. Mid-session the Claude Remote session ("fix camera monitor") asked, under a charter, for an outside control channel to a running auto engine (pick, strategy, status). Jeremy approved it, and it was built, verified and tested live.

## What shipped
- Reserve strategy: `src/claude_swap/{autoswitch.py,settings.py,cli.py,tui/autoview.py,tui/__init__.py}` plus `tests/test_autoswitch_reserve.py` and additions to test_settings/test_config_cli/test_cli/test_tui. Commits bf26cc2, d4234af.
- Engine control: `src/claude_swap/engine_control.py` (new), with hooks in autoswitch.py (`request_switch(on_result=)`, `withdraw_switch`, `apply_strategy`, `last_tick_at`), cli.py (`engine` verb, server start/stop around `run_loop`) and tui/autoview.py (server on mount/unmount, `_apply_engine_strategy`). Tests in `tests/test_engine_control.py`. Commit 424e7a2.
- Docs: both plans (marked built), `docs/decisions.md` (several 2026-11-01 entries), `docs/collaborations/2026-11-01-cswap-remote-control.md` (receiver log, closed).
- Full suite at last run: 2576 passed, 78 skipped.
- Live tests, all verified by `cswap status`: engine pick #4→#2→#4, engine strategy (applied and saved), and the no-engine `cswap switch` fallback #4→#2→#4.

## Dead-ends (do not retry)
- stdlib `deliver_challenge`/`answer_challenge`: the server-side recv is unbounded, and other users can open the Windows pipe read-only, so it leaks threads. Replaced with our own bounded HMAC handshake.
- Treating every connect `OSError` as "no engine": it brings back the hub's competing-switch fallback against a live engine. Only FileNotFound/ConnectionRefused (and ValueError for a bad address) mean no engine.
- A `save_settings` hunk to avoid writing the default "1": dead code (nothing in `src/` calls it). Reverted.
- Restoring a "specific" error for non-TTY bare `cswap --strategy best`: upstream never gave it, and a parity test pins that.
- Python `write_text` edits silently turned LF into CRLF here. Use Edit.

## Key files for next session
- Plans: `docs/plans/2026-11-01-consume-first-reserve.md`, `docs/plans/2026-11-01-engine-control.md`. Read these FIRST (§Contract in the second).
- `src/claude_swap/engine_control.py`: the whole control channel (transport, handshake, endpoint files, pick correlation).
- `docs/decisions.md`: the 2026-11-01 entries record every non-obvious call.
- `docs/collaborations/2026-11-01-cswap-remote-control.md`: what Remote consumes, and the test log.
- Memory touched: none.

## Running state
- Background processes: none of mine. Jeremy STOPPED his `cswap` dashboard for the fallback test; he restarts it himself.
- Dev servers / ports: none. The engine endpoint lives under `~/.claude-swap-backup/engine-control/` only while an engine runs.
- Worktrees / branches: `jeremy` only, pushed to origin at 16b57f2.

## Verification — how to confirm things still work
- `python -m pytest -q -n 4` (at BELOW_NORMAL): expect 2576 passed, 78 skipped.
- `cswap engine status --json`: exit 3 `no-engine` with cswap stopped; exit 0 `running` with host/mode/strategy once the auto screen or `cswap auto` is up.
- `cswap config get autoswitch.strategy`: `consume-first-reserve-jj`. `cswap config get autoswitch.reserveAccounts`: `1`.

## Deferred + open questions
- Deferred: upstream reconcile to 0.29.0 (current base is v0.26.0). Do it in this repo only, per CLAUDE.md §Reconciling, and confirm with Jeremy. Expect a conflict in the `_consume_first_app` helper in `tests/test_tui.py` and in the engine-control hooks.
- Deferred: the no-engine `config set` strategy fallback was not run live (it's the same exit-3 branch as the tested switch fallback).
- Open: none pending with Jeremy.
- Known accepted risks: a POSIX partial-frame stall (same user only); the endpoint file relies on the user profile ACL on Windows; restic backs up `engine-control/` (Jeremy: leave it).

## Pick up here
Nothing in flight. Next likely ask is the upstream 0.29.0 reconcile; start from CLAUDE.md §Reconciling with upstream.
