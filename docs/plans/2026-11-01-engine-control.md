# Plan — `cswap engine`: outside control of a running auto-switch engine

Date: 2026-11-01 · Branch: `jeremy` · Status: **built + verified live** (424e7a2; contract cswap-engine/0.1 + amendments)
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

**Additions found while building (0.1 amendments, shapes and codes unchanged)**
- Refusal reasons pass through every engine NoSwitch reason (e.g.
  `no-candidates`, `no-active-account`, `no-viable-target`, `all-exhausted`),
  plus `superseded` (exit 5): the TUI's own `m`/`n` replaced the pick first.
- Error codes (exit 1): `engine-stopped` (TUI rebuilt its engine on `s`/`l`,
  or the host quit, before the pick ran), `busy` (strategy only, since
  strategy has no exit 5), `bad-request`, `settings-error`, plus
  `multiple-engines`, `transport`, `engine-error`.
- Error shape: `{"contract", "op", "result": "error", "error", "message"}`;
  strategy errors add `persisted` and `live`. Pending: `{"contract", "op",
  "result": "pending", "message"}`. No-engine on pick/strategy: same shape as
  on status.
- `pick_pending` covers picks sent via `cswap engine` only, not a TUI `m`.
- **Exit 1 after `pick` does not prove nothing changed**: the engine can
  switch and then fail writing its own state (pre-existing upstream window),
  answering `engine-error` with no switch event. On any exit 1 or 4 after a
  pick, read `status` before acting.

**Fleet additions (2026-11-07, `docs/plans/2026-11-07-fleet-follow.md`; contract string unchanged)**
- `status` replies carry an additive top-level `fleet` block (`role`: `leader` |
  `worker` | `none`), always present; no emails in it. Keys under `engine` are unchanged.
- On a fleet worker, `strategy_source` can also be `fleet-lease` or
  `fleet-fallback`, and `strategy`/`reserve`/`threshold` report what the tick
  actually uses. A dev box (leader or no fleet) still reports only
  `settings` | `session`. The Claude Remote hub's `cswapctl` maps an unknown
  value to null (`_oneof`), so it tolerates these (checked 2026-11-07; it runs
  against the dev box, which is never a worker).
- New op `wake` (internal: `fleet receive`/`forget` call it through
  `engine_control.wake_engine()`): `{"result": "woken"}`, exit 0; not a
  `cswap engine` subcommand. Engines built before it answer `bad-request`.

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
