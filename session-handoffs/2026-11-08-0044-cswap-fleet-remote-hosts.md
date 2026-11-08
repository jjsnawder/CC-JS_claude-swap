# Handoff — cswap fleet: worker boxes follow the dev box (remote-hosts charter, closed)
Date: 2026-11-08 00:44  ·  Project: CC-JS_claude-swap  ·  Plan: `docs/plans/2026-11-07-fleet-follow.md`
Previous handoff: 2026-11-02-1926-reconcile-v0.29.0-cutover.md
Session: ad11ec78-22b5-42d1-9b45-02a1eebf6b04

## Firm decisions this session
- Charter from the env repo (remote-host control) approved as written; this side's log is public but generic (no host names, emails, IPs, roster). — The repo is public.
- Build on 0.29.0 now; the v0.30.0/v1.0.0 reconcile is a separate later session. — v1.0.0 drops keyring and turns on cross-account history sharing (slot-dir layout restic and cross-sessions read); it needs its own rulings.
- Transport is ssh + JSON CLI, fork-only, separable modules. — Rejected: the engine socket over TCP (reverses the 2026-11-01 no-port ruling).
- Workers FOLLOW the dev box's active account and live settings under a ~3 min lease. Only a LIVE dev-box engine leads (dry-run never does). When the dev box isn't leading, each worker runs `cswap auto` on its own with consume-first-reserve-jj. — Rejected: spreading workers across accounts, and a lower worker threshold (both proposed; Jeremy wants every box on his account).
- Hosts-menu overrides persist until cleared but apply only while the dev box leads. Workers follow onto the reserve account too.
- Settings master: `fleet push` on command, drift reported, never a silent overwrite.
- Needs-login alerts: cswap fires a hook (`fleet.alertCommand`); the env repo owns the email sender. — Rejected: cswap sending mail itself, and the env repo polling.
- Fallback test skipped (Jeremy: "We will not do the fall back test").
- Build-time calls are in `docs/decisions.md` (2026-11-07 entries):
  - a follower holds on its target until at-limit;
  - transient refusals retry next tick;
  - no warmup while following;
  - the leader and `fleet receive` are store-only;
  - the worker engine is woken only when the lease changes;
  - the ttl is a constant 180 s.

## Where it started
Peer session "dev1" (CC-JS_Claude_Environment) sent the charter `docs/collaborations/2026-11-07-cswap-remote-hosts.md` @ 88ffa8a in the env repo: the dev box sees and controls cswap on worker boxes. Planned with Jeremy, then built, verified, deployed to both boxes, extended with login alerts, live-tested, and closed.

## What shipped
- `cswap fleet` (lease, follow, leader thread, hosts, push, status contract `cswap-fleet/1`, overrides, forget): `src/claude_swap/fleet_lease.py`, `fleet_remote.py`, `fleet_leader.py`, plus `# fork: fleet` hunks in `autoswitch.py`, `engine_control.py` (top-level `fleet` status block, `wake` op), `cli.py`, `settings.py` (`fleet.fallbackStrategy`, `fleet.alertCommand`), and `transfer.py` (`import_usage_document`).
- Hosts screen (`h`): `src/claude_swap/tui/fleetview.py`, `tui/app.py`.
- Alert hook (`cswap-fleet-alert/1`, dedup in `fleet/alerts.json`, tree kill at 30 s, `cswap fleet alert-test`): `src/claude_swap/fleet_alerts.py`.
- Docs: `docs/fleet.md` (both contracts, timing, alerts, example worker unit); `docs/plans/2026-11-01-engine-control.md` (fleet additions); `docs/decisions.md`.
- Tests: `tests/test_fleet_*.py`, `tests/test_tui_fleetview.py`. Suite 3598 passed / 121 skipped.
- On `jeremy`: fleet ca61258, alerts b0001ef + 471f1d2, close c74c4ca, plan shipped 6542545. Pushed.
- Live operator changes (approved by Jeremy):
  - Dev box: worker host added (`fleet/hosts.json`), settings pushed, `fleet.alertCommand` set to the env repo's sender; the alert test was sent, exit 0.
  - Worker: `jeremy` pulled; `~/.config/systemd/user/cswap-auto.service` enabled; linger on; `updates.check false`.
- Collaboration log closed: `docs/collaborations/2026-11-07-cswap-remote-hosts.md`. The peer was asked to close its side and update its worker README and cswap cheatsheet.

## Dead-ends (do not retry)
- `pytest --noconftest` (deep did it by mistake): bypasses store isolation. Never use it. The live store was checked afterwards and was fine.
- Backgrounding the suite with a shell `&` inside an agent: it left a duplicate run, which I killed by PID. Use the Bash `run_in_background` parameter.

## Key files for next session
- Plan: `docs/plans/2026-11-07-fleet-follow.md`: read this FIRST (rulings, design, internal API, alert addendum).
- `docs/fleet.md`: the contracts and operator commands.
- `docs/collaborations/2026-11-07-cswap-remote-hosts.md`: the closed log, with every step and sha.
- Memory touched: none.

## Running state
- Background processes: none.
- Dev servers / ports: none.
- Worktrees / branches: worktrees removed. Remote branches `feat/fleet` and `feat/fleet-alerts` remain on origin, both merged; delete them when convenient.
- Live: the dev box dashboard engine is leader, pinned on account 2 (my pick at 00:41 after account 2's 5h reset). The worker follows on 2. Account 2's weekly window resets 04:00 CT.
- Scratch finished: none. The worktree `.tmp/` dirs went with the worktrees.

## Verification — how to confirm things still work
- `cswap fleet status --json`: the worker shows `following`, `on_target: true`, `drift: false`.
- `cswap engine status --json`: `fleet.role` is `leader`, with no emails in the output.
- `ssh dev1 'systemctl --user is-active cswap-auto.service'`: `active`.
- `cswap fleet alert-test --json`: `result: sent`, `exit_code: 0`, and a test email arrives.

## Deferred + open questions
- Deferred: the fallback test (Jeremy skipped it).
- Deferred: the upstream v0.30.0/v1.0.0 reconcile, a separate session. `fleet*.py` and its hunks need porting.
- Known minor, unfixed:
  - After an engine restart, one late lease from the old leader can steer a worker for one round.
  - `stop()` doesn't kill in-flight ssh children.
  - Warmup ordering and poll cadence on a worker read the box's own settings, not the lease's.
  - Alert stdin-writer thread can leak in an edge case (large payload + surviving grandchild).
  - `taskkill /T` stale-parent edge case.
  - Ctrl-C during `alert-test` can leave the sender running.
- Open: the dev box's slots 1 and 3 need a fresh `/login` (logins expire 2026-11-09 22:33Z and 2026-11-10 20:46Z). The alert email should report them once the dashboard runs the alert code; it was restarted after the fleet merge, but whether it restarted after the alert merge is unconfirmed.
- Open: the env sender calls `cswap list --json`, which can fetch usage while an engine runs. That's the env repo's choice; I suggested a store-only read.

## Pick up here
Nothing in flight. Next likely work: confirm the first real needs-login alert arrived for the dev box's slots 1 and 3, or start the v1.0.0 reconcile per `workflows/reconcile-upstream.md`.
