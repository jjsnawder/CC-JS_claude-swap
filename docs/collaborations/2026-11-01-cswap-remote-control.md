# Collaboration: cswap remote control (manual pick + strategy from Claude Remote)
Status: active
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
