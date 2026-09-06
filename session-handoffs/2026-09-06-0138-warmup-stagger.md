# Handoff — Warmup: staggered 5h windows via headless hellos (built, awaiting live slot trial)
Date: 2026-09-06 01:38  ·  Project: CC-JS_claude-swap  ·  Plan: `docs/plans/2026-09-06-warmup-stagger.md`
Previous handoff: `2026-09-04-1444-auto-screen-switch-now.md` (its open items are untouched: `n` live trial, three upstream PRs, reconcile/verify workflows)

## Firm decisions this session
- Wake policy = **staggered phases** (Jeremy's pick over keep-alive and fixed clock times): a cold account is pinged when its new reset lands at the midpoint of the largest gap between the other warm accounts' phases, tolerance half a spacing; `autoswitch.warmupStagger=false` degrades to keep-alive. Logged in `docs/decisions.md`.
- **Also a weekly Fable hello** when an account's Fable stamp is blank, driven by the existing `autoswitch.model`; once per account per label per day. Logged.
- Manual refresh (`p`, `cswap warm`) = **cold accounts only, then refetch all**; `--all` is the escape hatch. Ignores stagger; honours the 15-min cooldown unless `--all`.
- Hello mechanism = a real `claude -p` session, **never a raw API call with the OAuth token** (account-ban risk). The runner's argv is the contract: `-p hi --model <alias> --max-turns 1 --no-session-persistence --output-format json --system-prompt "Reply OK." --tools "" --setting-sources "" --strict-mcp-config --no-chrome --max-budget-usd 0.10`. Measured floor ~430–500 input tokens; `--bare` is unusable (kills OAuth), `--effort low` buys nothing. Jeremy: minimize tokens wherever possible.
- Active login is pinged through `paths.get_claude_config_home()` (follows `CLAUDE_CONFIG_DIR`, same as `current_account_number()`); `deep` RULED the env-ignoring default path would create a wrong-account bug. Do not change it. `setup_session` does NOT refuse the active login (the refusal is in `run()`); the warmup's own `number == active` check is the guard. Correction entry logged.
- Hellos spawn **after** the tick's switch decision; `_freshen_target` skips in-flight accounts; `switcher._perform_switch` refuses a target with a hello in flight (`_refuse_hello_in_flight`, process-wide registry in `warmup.py`); warmup refused inside a `cswap run` shell via `_refuse_session_shell()`. Logged.
- In-flight accounts stay in the planner as virtually warm at `now + period` (the plan said drop them; that caused a three-ping burst). Logged.
- `share=True` kept for the slot preparation: `_sync_sharing(share=False)` UNSHARES (removes managed copies), which would strip a live `cswap run` session's settings/skills.
- Worst-case cold wait is `period·(1 − 1/2N)` = 4 h 22 for N=4, not 3 h 45 (correction entry logged).
- Ships default-off (`autoswitch.warmupEnabled=false`), generic, PR-able from `v0.26.0` as `feat/warmup-stagger`. No `pyproject.toml` change → no reinstall.
- Jeremy retires the two Task Scheduler jobs (Claude 6AM Warmup, Claude 1101AM Warmup) in CC-JS_Claude_Environment himself once this behaves; nothing was changed there.

## Where it started
Jeremy asked for cswap, in auto-switch mode, to "say hi" to each of his four Team accounts with a cheap Haiku Claude Code instance so all four 5-hour windows cycle and spread across the day, plus a menu item to ping/refresh so 5h/7d/Fable reset stamps show after an Anthropic reset (they stay blank until the account is used). Must run hidden on the dev box. He answered three design questions (stagger / Fable ping / cold-only refresh), said "minimize token use", then went to sleep with his TUI running overnight and asked for an autonomous build, a handoff, and an emailed recap.

## What shipped
All on `jeremy`, pushed to `origin`, head 576a388:
- 9793624 — plan + four decision entries
- f02f116 — `src/claude_swap/warmup.py` (new): `window_state`, `plan_warmups`, `SubprocessPingRunner`, `redact_child_output`/`safe_error`, live-hello registry (`active_hellos`, `hello_in_flight`, `perform_hello`), `warm_now`; settings `warmupEnabled`/`warmupStagger` (`src/claude_swap/settings.py`); `tests/test_warmup.py` (new); `tests/conftest.py` autouse `_drain_live_hellos`; `tests/test_config_cli.py` key counts → `len(SETTING_SPECS)`
- 65c03e4 — `src/claude_swap/autoswitch.py`: `WarmupEvent`, `ping_runner` ctor param, drain+refetch hook in `_tick_inner`, `_warm_plan` stash, spawn in `tick()` → `_spawn_planned_warmups`, `_freshen_target` "warmup-in-flight", `_session_shell_ok`, `request_warm`, `_next_delay` clamp wrapper (`_cadence_delay` holds the old body), state `warmup.<num>`; `src/claude_swap/switcher.py` `_refuse_hello_in_flight` (+ one call in `_perform_switch`); `tests/test_autoswitch.py::TestWarmup`
- 8bb1b44 — TUI: `p` on auto screen (`action_ping_warm`, `check_action`) and dashboard (`app.action_warm_ping` worker), `_hello_blocks_switch` pre-check in `do_switch`/`action_switch_best`; `tests/test_tui.py`
- 71aa4e0 — `cswap warm [--dry-run] [--json] [--all]` (`cli.py::_warm_command`, exit 0/1/2); `tests/test_cli.py::TestWarmCommand`
- 576a388 — plan marked built with review/deep findings + verification results; six decision entries total
- Recap email sent 01:3x to jjsnawder@martingp.com via the internal relay (scratchpad `recap.html`, `send_recap.py`; not committed).
- `docs/model-usage.md` regenerated (gitignored): project total $64.82 extrapolated since the fork (Opus workers $37.54, Fable main+deep $27.21).

## Dead-ends (do not retry)
- Dropping in-flight accounts from the planner's eligible set — shrank N and let the next cold account bootstrap into the running hello's slot (three immediate pings).
- `share=False` for the warmup slot preparation — it unshares, not skips.
- Forcing a same-tick refetch after a hello — `UsageStore.reserve(respect_plans=False)` is `poll_due or stale`, no force path; the cooldown is the loop guard.
- `--bare` for the hello (disables OAuth); `--effort low` (no token saving).
- `"x"*5000` as a test filler for stderr — base64-shaped, gets redacted before the cap; use spaced words.
- Global mutable registry + daemon threads → xdist cross-test flake; fixed by `_settle` in the two spawning tests and the conftest drain fixture. Don't remove either.

## Key files for next session
- Plan: `docs/plans/2026-09-06-warmup-stagger.md` — read this FIRST (design, findings, verification ladder)
- `docs/decisions.md` — six 2026-09-06 entries
- `src/claude_swap/warmup.py` — the whole feature surface; docstrings carry the safety reasoning
- `src/claude_swap/autoswitch.py` — `_spawn_planned_warmups`, `_freshen_target` in-flight skip, `_session_shell_ok`, `request_warm`
- `src/claude_swap/switcher.py` — `_refuse_hello_in_flight` (the one upstream-body edit besides `_next_delay`)
- Memory touched: none

## Running state
- Background processes: none (tool-runner, review, deep subagents all completed)
- Dev servers / ports: none
- Worktrees / branches: `jeremy` clean at 576a388 (+ this handoff); `main` untouched at upstream. **Jeremy's overnight TUI (cswap.exe) predates every commit** — he relaunches himself; never kill by name. The live store has NOT been pinged through a slot dir yet.

## Verification — how to confirm things still work
- `powershell -NoProfile -Command '$p = Start-Process python -ArgumentList "-m","pytest","-q","-n","4","-p","no:cacheprovider" -PassThru -NoNewWindow -RedirectStandardOutput .tmp/pytest.out; $p.PriorityClass="BelowNormal"; $p.WaitForExit(); Get-Content .tmp/pytest.out | Select-Object -Last 4'` — expect 2261 passed, 78 skipped (~50 s); known flake `TestWatchScreen::test_late_normal_can_advance_usage_after_store_repaint`
- `cswap status` — live smoke (passed 00:5x)
- `cswap warm --dry-run` — plans only, spawns nothing; exit 2 when every account is warm (passed 01:1x, all four warm)
- One real hello via `SubprocessPingRunner().ping(config_dir=paths.get_default_claude_config_home(), model="haiku", cwd=<scratch>, timeout_s=120)` — ok, 259 in / 234 out, 5.5 s; no `~/.claude/projects/` entry, no `history.jsonl` entry
- Live slot trial (Jeremy): relaunch TUI → `cswap config set autoswitch.warmupEnabled true` → `p` in dry-run (`would-ping` lines) → live `p` (`pinged` lines; no window flash; terminal clean — the slot preparation runs a `claude auth status` probe with the TUI's console, first time from inside Textual)

## Deferred + open questions
- Deferred: the first real slot hellos — Jeremy's, in the morning (his instance was running; slot preparation rotates tokens).
- Deferred: residual cross-process race — a `cswap switch` in another terminal cannot see the in-process hello registry (same class as upstream's `run` vs `switch`); an on-disk lease would close it if it ever bites.
- Deferred: bookkeeping-only window in `_ping_worker` — the active hello can run as the new login if a switch lands between the re-read and Popen (no credential path involved).
- Deferred (carried from prior handoff): `n` live trial; three upstream PRs (+ now `feat/warmup-stagger`, cut from `v0.26.0`, code commits only); `workflows/reconcile-upstream.md` / `verify-change.md` unwritten.
- Open: after a `skipped` abort the start stamps (`lastPingAt`, `modelPings`) are left in place (builder's call: keeps the loop guard while the active account churns; cost = one deferred model hello). Revisit if a Fable stamp stays blank longer than expected.
- Open: `cswap warm --dry-run` inside a session shell refuses instead of previewing (correct, but surprising).

## Pick up here
Ask Jeremy how the live `p` trial went (dry-run lines, then real `pinged` lines, window/terminal clean, Fable stamps populated); if clean, he retires the two Task Scheduler jobs and we decide whether to cut `feat/warmup-stagger` upstream.
