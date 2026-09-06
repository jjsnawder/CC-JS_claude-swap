# Handoff — Warmup live trial: flag was off, now on; first staggered hello pending
Date: 2026-09-06 16:10  ·  Project: CC-JS_claude-swap  ·  Plan: `docs/plans/2026-09-06-warmup-stagger.md`
Previous handoff: `2026-09-06-0138-warmup-stagger.md` (the overnight build; everything there still stands)

## Firm decisions this session
- Jeremy's workflow is the contract: launch cswap → auto screen → `l` live → warmup runs and staggers on its own, **no extra step**. `p` is NOT his path: it hellos every cold account at once and aligns the resets, which is exactly what he does not want. Do not suggest `p` as the fix for "nothing warmed".
- The one missing piece this morning was `autoswitch.warmupEnabled` (default off). Set to `true` via `cswap config set autoswitch.warmupEnabled true` → lives in `~/.claude-swap-backup/settings.json` under `autoswitch` (camelCase). Read once at engine start; no `--warmup` CLI flag exists on `cswap auto` by design.
- `warmupStagger` stays at its default (true, absent from the file).

## Where it started
Jeremy reported cswap live all morning with accounts 1/2/3 still showing no 5h stamp (screenshot 15:52). Diagnosis: flag off (his TUI from 10:33 never had it), state file had no `warmup` section, `cswap warm --dry-run` correctly showed 4 warm / 1,2,3 would-warm. He asked for the expected behaviour, then relaunched (new `cswap.exe` PID 6220 at 16:05) and went live.

## What shipped
- `~/.claude-swap-backup/settings.json`: `autoswitch.warmupEnabled: true` (operator config, not in repo)
- No code changes this session. Branch `jeremy` unchanged at 69ea77d.

## Dead-ends (do not retry)
- Bash `run_in_background` for the ~75-min wait — capped at 10 min; used Monitor (1 h cap) instead.
- Earlier statement "first hello comes within minutes" was wrong for today's state (one warm anchor → the cold accounts wait for their phase slots); corrected to Jeremy.

## Key files for next session
- Plan: `docs/plans/2026-09-06-warmup-stagger.md` — read this FIRST
- `session-handoffs/2026-09-06-0138-warmup-stagger.md` — the build handoff (decisions, dead-ends, verification ladder)
- `src/claude_swap/warmup.py` `plan_warmups` — the stagger math if the observed times disagree with the table below
- `~/.claude-swap-backup/autoswitch_state.json` — `warmup.<num>.lastPingAt/lastPingModel/lastResult/failures` appears after the first hello
- Memory touched: none

## Running state
- Background processes: Monitor task `bjmaqwb17` (polls the state file every 60 s, emits one line when the first `lastPingAt` appears, 1 h timeout → expires ~17:08, BEFORE the expected 17:15 hello; re-arm if still needed). Bash background `btq3oim9y` (same check, 10-min cap) — expired/irrelevant. Kill: TaskStop by id.
- Dev servers / ports: none
- Worktrees / branches: `jeremy` clean at 69ea77d; Jeremy's live TUI is `cswap.exe` PID 6220 (started 16:05, LIVE, has the flag) — never kill by name.

## Verification — how to confirm things still work
- Expected first hellos (anchor = Account-4 resetting 16:00 / 21:00): Account-3 ≈ 17:15 (reset 22:15), Account-1 ≈ 18:30 (23:30), Account-2 ≈ 19:45 (00:45). Each shows as a `pinged` line in the TUI log and a 5h stamp in the quota monitor a tick later.
- `python -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude-swap-backup/autoswitch_state.json'))).get('warmup'))"` — `None` until the first hello, then a dict keyed by slot number
- `cswap warm --dry-run` — read-only planner view (exit 2 when all warm)
- `cswap config get autoswitch.warmupEnabled` — `true`

## Deferred + open questions
- Deferred: confirm on the first live hello that no window flashes and the TUI terminal stays clean (slot preparation runs a `claude auth status` probe with the TUI's console).
- Deferred (carried): `n` live trial; upstream PRs incl. `feat/warmup-stagger`; `workflows/reconcile-upstream.md` / `verify-change.md`; retiring the two Task Scheduler warmup jobs once this behaves for a day.
- Open: if the 17:15 hello does not appear, first suspects are the freshness guard (candidate entries older than `SERVE_TTL_S` are only fetched when their poll plan is due) and the tolerance window — check `warmup.<num>` state and the TUI log for `would-ping`/`skipped` lines before touching code.

## Pick up here
Ask Jeremy whether the ~17:15 Account-3 hello appeared (`pinged` line, 5h stamp, clean terminal); if not, read the state file and the TUI log before changing anything.
