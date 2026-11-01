# Collaboration: cswap remote control (manual pick + strategy from Claude Remote)
Status: closed 2026-11-01
Projects: CC-JS_Claude_Remote ↔ CC-JS_claude-swap     This file: CC-JS_claude-swap's side (receiver)
Charter: D:\Dev Projects\CC-JS_Claude_Remote\docs\collaborations\2026-11-01-cswap-remote-control.md @ 0a298f9

Scope as written in the charter (approved without narrowing). Contract this side
offers: `docs/plans/2026-11-01-engine-control.md` §Contract.

## Approvals
- 2026-11-01 CC-JS_claude-swap: Jeremy chose "Approve as written (Recommended)"
  (AskUserQuestion in this session, after reading the charter @ 0a298f9).

## Log
- 2026-11-01 12:21 CT · received charter request from the CC-JS_Claude_Remote session ("fix camera monitor")
- 2026-11-01 12:22 CT · Jeremy approved as written in this session
- 2026-11-01 12:25 CT · draft contract cswap-engine/0.1 written (plan file), sent to Remote
- 2026-11-01 12:40 CT · built cswap engine (uncommitted); deep verify: ship after fixes; fixes in progress
- 2026-11-01 · Remote asked kill-safety of `cswap switch` (hub 15 s timeout + taskkill /T) and `engine pick`; answered: per-file writes atomic (temp + os.replace), multi-step switch not (kill between steps = mixed but recoverable state), suggest 30 s, after a kill check status and re-run once; engine pick kill only abandons the wait
- 2026-11-01 · engine control committed 424e7a2 (deep verify + 2 review rounds); live `cswap engine status --json` = no-engine/exit 3 (running dashboard predates the change); told Remote: commands installed, contract amendments (superseded, error codes, transport != no-engine, fall back only on exit 3), hold status test until restart
- 2026-11-01 · Jeremy: dry-run test first (he restarts the dashboard; live pick needs a fresh OK); restic exclude for engine-control/: "Leave it"
- 2026-11-01 · dry-run engine up (TUI pid 60884); my check: status running/dry-run, pick 2 -> would-switch, active unchanged. Remote's test: hub direct + HTTP routes (GET /api/cswap, POST /api/cswap/switch) would-switch 4->2 in ~1 s, bad slot 400, active unchanged; contract 0.1 + amendments consumed as written
- 2026-11-01 · Jeremy approved live test (#4 -> #2 -> #4) after pressing `l`. Remote's hub POST /api/cswap/switch: 4->2 switched in 2.6 s (verified here: engine active 2 pinned 2, live login Account-2), 2->4 switched in 1.7 s (verified: active 4 pinned 4, live login Account-4). No retries, #1 and strategy untouched.
- 2026-11-01 · contract `cswap-engine/0.1` + amendments is the version the hub consumes (string stays "cswap-engine/0.1"). NOT yet verified live: `engine strategy` (persists to settings) and the hub's no-engine fallback (`cswap switch` / `config set`). Collaboration stays open until Jeremy rules on those.
- 2026-11-01 · Test A (Jeremy-approved): hub set strategy consume-first-reserve-jj via engine -> applied, live; verified here strategy_source flipped session->settings, config get = consume-first-reserve-jj, reserve 1 (kept as Jeremy's saved default). Suggested hub pass `persisted` through.
- 2026-11-01 · Test B (Jeremy stopped cswap): status no-engine/exit 3, endpoint file removed. Hub fallback `cswap switch` 4->2 (2.1 s, verified Account-2) and 2->4 (2.2 s, verified Account-4).
- 2026-11-01 · Done when met (engine pick, engine strategy, no-engine switch fallback all verified live; `config set` fallback not run live, same exit-3 branch). Closed. Jeremy's cswap auto is stopped; restart is his.
