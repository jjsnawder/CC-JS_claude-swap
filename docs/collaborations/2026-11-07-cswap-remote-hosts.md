# Collaboration: cswap remote-host control (the dev box drives cswap on worker boxes)
Status: closed 2026-11-08
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
- 2026-11-07 20:02 CT · Planning settled with Jeremy (sequencing, transport, follow-the-dev-box coordination, settings master); plan `docs/plans/2026-11-07-fleet-follow.md` approved ("Approve, start building"); router contract will be `cswap fleet status --json`, `cswap-fleet/1`. Building on a feature worktree; merge and worker deploy stay gated.
- 2026-11-07 20:53 CT · Built on `feat/fleet` (pushed): suite 3549 passed / 121 skipped; review x2, deep (ship after fixes, fixed), scratch two-store trial 8/8. Awaiting Jeremy: merge to `jeremy`, worker deploy.
- 2026-11-07 20:57 CT · Jeremy: "Merge now"; `jeremy` fast-forwarded to ca61258 and pushed (dev-box deploy; smoke test: 4 slots ok, `fleet status` cswap-fleet/1). Worker box pulled to ca61258 (Jeremy: deploy code now; host entry + permanent `cswap auto` after its logins). Worker has no logins yet; live follow test pending.
- 2026-11-07 21:19 CT · Jeremy: "Go: all four, then test". Worker logins verified (4 slots ok). Worker host added on the dev box; settings pushed (4 keys, no identity mismatch); worker `updates.check` false; worker `cswap auto` installed as a systemd user unit with linger. The worker followed the leader 4 -> 2 at 21:17:47. `fleet status`: worker following, on_target, drift false. Done-when #3 met. Still to run: a leader switch the worker follows, and the fallback test. Cosmetic: the leader row reports on_target false (should be null).
- 2026-11-07 21:22 CT · Jeremy: needs-login alerts split ("Split: cswap hook, env sends"). Building `fleet.alertCommand` + `cswap-fleet-alert/1` on feat/fleet-alerts; proposed the sender to the env-repo peer as a scope addition for Jeremy to confirm there.
- 2026-11-07 · Received from env-repo peer: alert sender built (env @ e78eaee), Jeremy approved the scope there; proposed `fleet.alertCommand` = python.exe + infrastructure/claude-worker/fleet-alert/send_alert.py (exit 0 sent/none, 1 retry). Setting it waits on the hook merge + Jeremy here.
- 2026-11-07 21:46 CT · Jeremy: "Merge + set + test". Alert hook on jeremy (b0001ef, 471f1d2; review x2, suite 3598 passed). Dev box `fleet.alertCommand` set to the env sender; `cswap fleet alert-test` -> sent, exit 0. Real alerts start when the dashboard restarts onto this code.
- 2026-11-08 00:41 CT · Live switch tests passed. The engine auto-switched 2 -> 4 at 00:20 and the worker followed. Test pick 4 -> 3 at 00:24:37, worker on 3 by 00:24:41. Pick back to 2 at 00:41:00, both boxes on 2 by 00:41:08. Fallback test skipped (Jeremy: "We will not do the fall back test").
- 2026-11-08 · Closed (Jeremy: "Close this out"). Done when: plan approved (237abda); feature live on both boxes (jeremy ca61258, alerts b0001ef/471f1d2); one command shows both hosts (`cswap fleet status`); router contract cswap-fleet/1 + alert contract cswap-fleet-alert/1 in docs/fleet.md. Asked the env-repo peer to close its side.
