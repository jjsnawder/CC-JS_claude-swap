# Collaboration: cswap remote-host control (the dev box drives cswap on worker boxes)
Status: active
Projects: CC-JS_Claude_Environment ↔ CC-JS_claude-swap     This file: CC-JS_claude-swap's side (receiver)
Charter: CC-JS_Claude_Environment `docs/collaborations/2026-11-07-cswap-remote-hosts.md` @ 88ffa8ae36033f4e4b3696705db9f0742d76070a

Approved as written: Purpose through Done when live in the charter (private repo) and are not
repeated here.

**Public-repo rule for this file and everything the collaboration produces here:** the charter's
"Operator facts" never come into this repo. No host names, IPs, account emails or slot roster.
Remote hosts are "the dev box" (master) and "worker boxes" (the first one, then future ones).

## Approvals
- 2026-11-07 CC-JS_claude-swap: "Approve as written" (AskUserQuestion, this session). Same
  question: keep this side's log public but generic ("Public, generic").

## Log
- 2026-11-07 18:59 CT · Received the charter request from the env-repo peer (charter @ 88ffa8a).
  Read the charter at that sha; the only later change (ca3bbef) is a log line on its side.
- 2026-11-07 19:01 CT · Jeremy approved as written in this session; this log created. Next:
  planning session with Jeremy, `docs/plans/<date>-<slug>.md` before any code.
- 2026-11-07 19:02 CT · Sent the approval and this log's sha (0caa359) to the env-repo peer; no reply requested.
- 2026-11-07 19:40 CT · Planning settled with Jeremy (sequencing, transport, follow-the-dev-box coordination, settings master); plan `docs/plans/2026-11-07-fleet-follow.md` approved ("Approve, start building"); router contract will be `cswap fleet status --json`, `cswap-fleet/1`. Building on a feature worktree; merge and worker deploy stay gated.
- 2026-11-07 21:20 CT · Built on `feat/fleet` (pushed): suite 3549 passed / 121 skipped; review x2, deep (ship after fixes, fixed), scratch two-store trial 8/8. Awaiting Jeremy: merge to `jeremy`, worker deploy.
- 2026-11-07 21:58 CT · Jeremy: "Merge now"; `jeremy` fast-forwarded to ca61258 and pushed (dev-box deploy; smoke test: 4 slots ok, `fleet status` cswap-fleet/1). Worker box pulled to ca61258 (Jeremy: deploy code now; host entry + permanent `cswap auto` after its logins). Worker has no logins yet; live follow test pending.
