# Plan — Warmup: staggered 5-hour windows across all accounts

Date: 2026-09-06 · Branch: `jeremy` · Status: **planned — building**
Depends on: `2026-09-04-auto-screen-switch-now.md` (shipped).

## Ask (Jeremy, 2026-09-06 evening)
While cswap is open in auto-switch mode, keep every managed account's 5-hour
window running by sending a tiny headless Claude Code hello to any account whose
window has lapsed, so the four accounts' reset times are spread across the day.
Plus a manual "ping refresh" (TUI hotkey + CLI) that hellos the cold accounts
and refetches usage, because after an Anthropic reset the 5h/7d/Fable reset
stamps stay blank until the account is actually used. Pings run hidden — no
window may appear on the dev box. Token use per ping is to be minimized.

Answers that shaped the design (asked and answered 2026-09-06):
- Wake policy: **staggered phases** (not plain keep-alive, not fixed clock times).
- Model windows: **also a weekly Fable ping** when the Fable window is cold.
- Manual refresh: **ping cold accounts only, then refetch all**.
- Ping mechanism: a real Claude Code `-p` session, never a raw API call with the
  OAuth token (third-party use of those tokens is what Anthropic has cut
  accounts off for). Decided by the orchestrator, stated to Jeremy.

## Findings (verified at jeremy@34fc7da, 2026-09-06)
- Today's warmup is outside cswap: two Task Scheduler jobs (06:00, 11:01) run
  `echo warmup | claude -p` as the *global* login. One account only, nothing
  read back. Jeremy retires them himself once this ships.
- Usage: `oauth.build_usage_result` writes `resets_at` only when the API sends
  it; a never-used window has a pct and no stamp. `autoswitch._five_hour_reset_ts`
  returns `None` for absent **or past** stamps. "Cold" = 5h stamp absent or
  ≤ now. Scoped per-model windows come from `usage["scoped"]` via
  `oauth.relevant_windows(usage, models)`; the engine's parsed labels are
  `self._models` (`Fable`, `Opus`, `all`).
- Engine (`autoswitch.py`): thread loop `run_loop` → `tick` → `_tick_inner`,
  shared by the TUI auto screen and `cswap auto` (`--once` included).
  Non-active accounts are refetched one per tick, stalest first, roughly every
  300–600 s; `usage_entries_by_account(fetch={num}, scheduled=False)` beats the
  serve TTL for one account but never backoff / dead-token / lease. State file
  `autoswitch_state.json` via `_mutate_state`. Events are frozen dataclasses
  rendered by one `event_text` in `tui/autoview.py` (`_EVENT_ROLES`).
- Launch (`session.py`): `SessionManager.setup_session(identifier, share)` does
  the whole gate / validate / bootstrap / share-copy sequence and returns
  `(session_dir, num, email)` **without** exec'ing — the entry point for a
  headless spawn. It **refuses the active default login** (a slot copy of the
  live login would drift). Credential sync is one-way store → slot; the slot's
  `.credentials.json` rotates its own token family; usage fetch already prefers
  the slot file read-only. `AUTH_OVERRIDE_ENV_VARS` are scrubbed; env gets
  `CLAUDE_CONFIG_DIR=<slot>`. Nothing in the package runs Claude headless or
  hides a window today; the Windows branch is a foreground `subprocess.run`.
- Eligibility predicates exist: `switchable_account_numbers()` (drops
  `disabled`), `_account_kind == "api_key"` / `USAGE_API_KEY` sentinel
  (`SessionManager._ensure_not_api_key` raises for these), state
  `quarantine.<num>`, `UsageEntry.in_backoff()` / `token_dead()`.
- Tests: `EngineHarness` in `tests/test_autoswitch.py` fakes
  `usage_entries_by_account` with a canned dict (a refetch-after-ping test
  needs `side_effect`). `tests/test_real_store_guard.py` installs an audit hook
  that refuses writes to the real store from any thread — a real `claude`
  child in tests would trip or escape it, so **the spawn seam is injected and
  never real in tests**.
- Measured ping shape (orchestrator, active login, 2026-09-06):

  | Command variant | input | cache-create | output | cost |
  |---|---|---|---|---|
  | `-p hi --model haiku --max-turns 1 --no-session-persistence --output-format json --system-prompt "Reply OK."` | 10 | 21,626 | 64 | $0.044 |
  | same + `--tools "" --setting-sources ""` | 431 | 0 | 79 | $0.0008 |
  | same + `--strict-mcp-config --no-chrome --effort low --max-budget-usd 0.05` | 454 | 0 | 139 | $0.0011 |
  | minimal shape, `--model fable` (alias verified, returns `claude-fable-5-1`) | 494 | 0 | 15 | $0.0057 |

  The floor is ~430 input tokens (fixed scaffolding). `--bare` is unusable: it
  disables OAuth. `--effort low` bought nothing.

## Design

### Module `src/claude_swap/warmup.py` (new, separable, default-off)
Three parts, all unit-testable without a network or a child process:

1. **Cold detection.** `window_state(usage, now, models)` → per account:
   `five_hour_cold: bool` (stamp absent or ≤ now), `cold_models: list[str]`
   (labels in `models` whose scoped window has no stamp or a past one; `all`
   = every scoped entry present), `at_limit: bool` (any relevant window pct ≥
   100). Reads the raw `usage["five_hour"].get("resets_at")` — not
   `_five_hour_reset_ts`, which folds "past" into "unknown" (here they mean the
   same thing, but the raw read keeps the rule explicit).

2. **Stagger planner** — pure: `plan_warmups(now, accounts, *, period=5h,
   stagger=True) -> list[WarmupDecision]`.
   - Eligible set E = accounts passed in (caller already dropped disabled,
     API-key, quarantined, token-dead, in-backoff, at-limit). N = |E|,
     `spacing = period / N`, `tolerance = spacing / 2`.
   - Warm accounts contribute a phase `resets_at mod period`.
   - Cold accounts are processed in slot order. For each: if no warm phases →
     `ping_now` (bootstrap anchor). Else find the largest circular gap between
     warm phases; `target` = its midpoint; `d` = signed offset of `now +
     period` from `target` (mod period, in `[-period/2, period/2)`). If
     `|d| ≤ tolerance` → `ping_now`; else `wait_until(now + ((target - (now +
     period)) mod period))`. A scheduled account then counts as warm with the
     virtual phase `(ping_time + period) mod period` for the next cold one.
   - Worked case (4 cold at 08:00): pings at 08:00, 10:30, 09:15, 11:45 →
     resets 13:00 / 14:15 / 15:30 / 16:45, 75 min apart. Steady state: an
     account whose window lapses on phase has `|d| ≈ ping latency` → pings
     immediately. Worst-case cold wait = `period·(N−1)/N` = 3 h 45 for N=4;
     a cold account is still fully usable (a switch onto it starts a fresh
     window), so the wait costs reset-nearness, never quota.
   - `stagger=False` → every cold account is `ping_now` (keep-alive).
   - Model pick: `ping_now` uses the first `cold_models` label if any (one call
     starts both the 5h and the model window), else `haiku`. A 5h-**warm**
     account with a cold model window gets a `ping_now(model)` immediately —
     it does not disturb the phase because the window is already running.
     Guard: one model ping per `(account, label)` per 24 h from state.

3. **Runner.** `PingRunner` protocol `ping(config_dir: Path, model: str,
   cwd: Path, timeout_s: float) -> PingResult`; `SubprocessPingRunner`
   spawns `shutil.which("claude")` with exactly:
   ```
   -p hi --model <alias> --max-turns 1 --no-session-persistence
   --output-format json --system-prompt "Reply OK." --tools "" --setting-sources ""
   --strict-mcp-config --no-chrome --max-budget-usd 0.10
   ```
   env = `os.environ` minus `AUTH_OVERRIDE_ENV_VARS`, plus `CLAUDE_CONFIG_DIR`
   set explicitly (slot dir, or `paths.get_claude_config_home()` for the
   active login). stdin `DEVNULL`, stdout/stderr piped, `cwd=<backup>/warmup/`
   (created once; no transcripts land anywhere thanks to
   `--no-session-persistence`). Windows: `creationflags = CREATE_NO_WINDOW |
   BELOW_NORMAL_PRIORITY_CLASS`; POSIX: `start_new_session=True`. Timeout
   120 s → `proc.kill()` (our own handle, i.e. by PID). Result parses the JSON:
   `ok = subtype == "success" and not is_error`; records model, input tokens
   (input + cache-create), output tokens, `total_cost_usd`, duration; failures
   carry rc and the first 200 chars of stderr. **Nothing from env or the config
   dir is ever logged.** Model alias map: label lowercased (`Fable`→`fable`,
   `Opus`→`opus`); default `haiku`.

### Engine integration (`autoswitch.py`)
- New settings on `AutoSwitchSettings` + `SETTING_SPECS`: `warmupEnabled`
  (bool, default **false**) and `warmupStagger` (bool, default true). Models
  for model-window pings = the existing `autoswitch.model`.
- `AutoSwitchEngine.__init__` takes `ping_runner: PingRunner | None`
  (default `SubprocessPingRunner()`); tests inject a fake.
- Per tick, after the usage collection:
  1. Drain finished ping threads → `WarmupEvent(action="pinged"|"failed")`,
     update state, and force-refetch that account this tick
     (`usage_entries_by_account(fetch={num}, scheduled=False)`), so the new
     stamp shows within one tick.
  2. Build the eligible set: active + switchable, minus API-key, quarantined,
     token-dead, in-backoff, at-limit, ping-in-flight, warmup-backoff.
     **Freshness guard:** an account whose entry is older than `SERVE_TTL_S`
     and looks cold is *not* pinged this tick — it is added to the fetch set
     and re-evaluated next tick. Never ping on a stale read.
  3. `plan_warmups(...)`. `ping_now` → start a daemon thread running the
     runner (dedupe: one in flight per account). Dry-run engine → emit
     `WarmupEvent(action="would-ping")` only. `wait_until` → remember the
     earliest deadline; `_next_delay` clamps the sleep to it, and to the
     nearest eligible 5h stamp expiry + 60 s so lapses are confirmed promptly.
- Active account: pinged through `paths.get_claude_config_home()` (the default
  login), never a slot — `setup_session` refuses it by design. Other accounts:
  `SessionManager(switcher).setup_session(num, share=True)` → slot dir. Note
  this reuses the exact `cswap run` preparation (bootstrap + share copy).
- Failure policy per account: 30 min backoff after a failed ping; three
  consecutive failures → 24 h backoff + `WarmupEvent(action="failed")` at
  warn severity. Never raises out of the tick (`tick()` already shields).
- State (`autoswitch_state.json`, via `_mutate_state`):
  `warmup: {<num>: {lastPingAt, lastPingModel, lastResult, failures,
  backoffUntil, modelPings: {label: ts}}}`.
- Manual: `request_warm()` (Event + wake, like `request_switch`). The next tick
  pings **every cold eligible account now** (stagger ignored — the user asked
  for data), then force-refetches all eligible accounts once results land.
- `warm_now(switcher, *, models, runner, dry_run, force_all=False)` is a
  standalone function (no engine) used by the CLI and the dashboard; it runs
  pings synchronously in a bounded pool, then refetches all.

### TUI
- Auto screen: `Binding("p", "ping_warm", "Ping/warm")` → `engine.request_warm()`
  + muted log line `— warmup requested —`; inert in threshold-adjust mode
  (mirror `n`). `WarmupEvent` renders muted; failures `sev_warn`. `_FakeEngine`
  gains `request_warm()` + counter.
- Dashboard: `Binding("p", "app.warm_ping", "Ping/warm")` → worker runs
  `warm_now(...)` then `refresh_full`.

### CLI
`cswap warm [--dry-run] [--json] [--all]`: cold-only by default; `--all` pings
every eligible account (Jeremy's rejected default, kept as a flag); prints one
line per account then the usage table. Exit 0 ok, 1 error, 2 nothing to do.

### Upstream shape
Generic (any multi-account user benefits), default off, in its own module with
narrow hooks → PR-able from `v0.26.0` as `feat/warmup-stagger`. Windows flags
are platform-conditional. No `pyproject.toml` change → no reinstall.

## Blast radius
Spawns Claude with per-slot credentials via the `cswap run` preparation path
and adds a new writer of `autoswitch_state.json`. No change to credential
formats, store layout, or the copy list. `--no-session-persistence` means no
transcript reaches `sessions/<slot>/projects/`, so restic and cross-sessions see
nothing new; the `warmup/` cwd dir under the backup root is a new, empty,
non-secret path. **`deep` verifies before commit**, briefed adversarially:
try to show a ping can corrupt or churn slot credentials, ping the wrong
account, trip or escape the real-store guard from a test, leak env or paths
into an event, or show a window on Windows.

## Tests
- `tests/test_warmup.py` (new): cold detection (absent / past / future stamp,
  scoped models, `all`, at-limit); planner (4-cold bootstrap → 0/150/75/225
  min; on-phase lapse → now; small negative drift → now; large negative →
  wait; N shrinks → spacing grows; stagger off → all now; model-cold picks the
  model; 5h-warm + model-cold → immediate model ping); runner with a patched
  `subprocess.Popen` (argv exact, env scrub + `CLAUDE_CONFIG_DIR`, Windows
  flags when `sys.platform == "win32"`, JSON parse, timeout → kill, non-JSON →
  failure, stderr truncation, no env in result).
- `tests/test_autoswitch.py::TestWarmup`: disabled by default (no runner call);
  enabled → ping fires with fake runner, event emitted, forced refetch via
  `side_effect`, state written; in-flight dedupe; stale-entry guard; skips
  API-key / quarantined / at-limit; dry-run → `would-ping` only; active account
  uses the default config dir, others the slot dir (fake `setup_session`);
  failure backoff; `request_warm` pings all cold now; `_next_delay` clamp.
- `tests/test_tui.py`: `p` binding on both screens, log line, inert while
  adjusting; `tests/test_cli.py`: `warm` dispatch, `--dry-run`, exit codes;
  `tests/test_settings.py`: the two specs round-trip.
- Full suite at BELOW_NORMAL, `-n 4`; baseline 2132 passed / 77 skipped.

## Verification (orchestrator)
1. Suite green. 2. `cswap status` live smoke. 3. `cswap warm --dry-run` live
(read-only planning against real usage). 4. One real ping through
`SubprocessPingRunner` against the **active login only** (no slot touched) to
prove the hidden spawn + JSON parse. 5. The first real slot pings are Jeremy's
in the morning: relaunch the TUI, `cswap config set autoswitch.warmupEnabled
true`, `p` on the auto screen in dry-run, then live.

## Status log
- 2026-09-06 evening — plan written; Jeremy asleep, build proceeds autonomously.
