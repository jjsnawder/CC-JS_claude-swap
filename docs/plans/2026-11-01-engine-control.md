# Plan — `cswap engine`: outside control of a running auto-switch engine

Date: 2026-11-01 · Branch: `jeremy` · Status: **draft contract sent; build not started**
Collaboration: `docs/collaborations/2026-11-01-cswap-remote-control.md` (Claude Remote's hub consumes it).
Shape: fork-only, separable module (`src/claude_swap/engine_control.py`) + one CLI verb.

## Ask
Claude Remote must (1) manually pick an account through the same engine path
as `m`, (2) set the strategy live and persisted, and (3) read engine status.
Today `request_switch(target=N)` is in-process only.

## Contract — `cswap-engine/0.1` (DRAFT; becomes 1 when shipped)
Every command prints exactly one JSON object on stdout with `--json` (a human
line without it). No emails or tokens in any payload; accounts are numbers.

**Exit codes (all three commands)**
| Code | Meaning |
|---|---|
| 0 | done (status: engine running; pick: switched or dry-run preview; strategy: applied) |
| 1 | error (`error` field: `multiple-engines`, `transport`, `engine-error`, ...) |
| 2 | usage error (argparse; bad strategy name, non-numeric account) |
| 3 | no engine running: nothing was done; the hub falls back to plain CLI |
| 4 | pending: request delivered, no outcome within `--timeout`; poll status |
| 5 | refused by the engine (pick only); `reason` says why |

**`cswap engine status --json`**
```json
{"contract": "cswap-engine/0.1", "op": "status", "result": "running",
 "engine": {"pid": 1234, "host": "auto" , "mode": "live", "strategy": "consume-first-reserve-jj",
            "strategy_source": "settings", "reserve": ["1"], "threshold": 90.0,
            "active": "4", "pinned": null, "pick_pending": false,
            "started_at": "2026-11-01T18:00:00Z", "last_tick_at": "2026-11-01T18:21:00Z"}}
```
`host`: `auto` (`cswap auto` loop) or `tui` (dashboard auto screen). `mode`:
`live` | `dry-run`. `strategy_source`: `settings` | `session` (a `s` or
launch-flag override). No engine → `{"contract": ..., "op": "status",
"result": "no-engine"}`, exit 3.

**`cswap engine pick N --json [--timeout SECONDS]`** (default 30)
Synchronous: delivered as `request_switch(target=N)`; the CLI waits for the
tick that consumes it (fresh usage fetch included, normally a few seconds).
```json
{"contract": "cswap-engine/0.1", "op": "pick", "result": "switched",
 "from": "4", "to": "2", "pinned": "2", "dry_run": false, "message": "switched #4 -> #2 (manual pick)"}
```
- Dry-run engine: `"result": "would-switch"`, `"dry_run": true`, exit 0, nothing changed.
- Refused, exit 5: `"result": "refused", "reason": "<code>", "message": "<human line>"`.
  Reason codes are the engine's own `m` refusals: `already-active`,
  `target-unavailable`, `target-quarantined`, `target-over-threshold`,
  `target-usage-unknown`, `target-busy`; plus `no-qualifying-candidate` /
  `no-comparison` if the tick's fresh data rules it out.
- Exit 4 `"result": "pending"` if the tick outlives `--timeout`; the pick may
  still land, so check `status` (`pick_pending`, `active`, `pinned`).
- An API-key target switches but is never pinned (`"pinned": null`), as with `m`.

**`cswap engine strategy best|consume-first|consume-first-reserve-jj --json`**
Persists `autoswitch.strategy` to settings AND applies it to the running
engine (held pick carried over, as the TUI's `s` does).
```json
{"contract": "cswap-engine/0.1", "op": "strategy", "result": "applied",
 "strategy": "best", "persisted": true, "live": true}
```
No engine → exit 3 and nothing persisted (the hub's fallback is
`cswap config set autoswitch.strategy X`).

**Concurrency:** one request at a time per engine; a second pick while one is
pending is answered `refused` / `reason: "busy"`. Two engines running at once
(`cswap auto` and a TUI auto screen) → exit 1, `error: "multiple-engines"`.

## Transport (this side's choice; invisible to the hub)
Box-local only. Working choice: `multiprocessing.connection` Listener — a named
pipe on Windows, a Unix socket elsewhere — with a random authkey. The engine
writes a small user-only endpoint file under the backup root on start and
removes it on stop (stale file = dead pid → treated as no engine). The hub
never reads it; only `cswap engine` does. To verify during build: Listener
thread lifecycle inside the TUI worker, and the strategy change on a `cswap
auto` engine (settings are read at construction; may need an
`apply_strategy` mirroring `apply_threshold`).

## Blast radius
Touches the switch path's entry point (`request_switch`) and adds a listener
to a long-running process. No credential, store-layout or slot-copy change.
`deep` verifies (fresh context) because it is the switch path; `review` before
commit. Live test: Jeremy's OK first, dry-run engine before live.

## Tests
Contract tests per exit code and payload shape with a fake engine; a real
Listener round-trip in-process; stale endpoint file; multiple engines;
pending/timeout; strategy persisted + live; no-engine writes nothing.
