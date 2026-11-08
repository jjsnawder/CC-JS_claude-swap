# Plan — `cswap fleet`: worker boxes follow the dev box

Date: 2026-11-07 · Base: `jeremy` @ v0.29.0 + fork · Branch: `feat/fleet` (worktree) · Status: **DRAFT, awaiting Jeremy's approval**
Collaboration: `docs/collaborations/2026-11-07-cswap-remote-hosts.md` (charter in the env repo @ 88ffa8a).
Shape: fork-only, separable modules + small hooks; ssh + JSON CLI. Contract `cswap-fleet/1`.

Public repo: no host names, addresses, emails or slot rosters here or in code, tests or fixtures.
"Dev box" = the master; "worker" = any remote box named in the dev box's fleet config.

## Ask (Jeremy, 2026-11-07)
> "I'm wanting to have all sessions across all worker boxes to follow what I have set and the
> active session on my main dev box. I want to have a menu where I can manually override this.
> If cswap is ever not running on the dev box I want it revert to the consume first, reserve
> last with auto running all the time."

## Rulings (2026-11-07, this session; also in `docs/decisions.md`)
| # | Question | Ruling |
|---|---|---|
| 1 | Sequencing vs upstream v1.0.0 | Build on 0.29.0 now; the v1.0.0 reconcile is a separate later session. |
| 2 | Transport / shape | ssh + JSON CLI, fork-only, separable (could be offered upstream later). |
| 3 | Coordination | Workers **follow** the dev box's active account and settings (not spread out). |
| 4 | Who leads | Only a **live** engine on the dev box (`cswap auto`, or the TUI auto screen after `l`). Dry-run or no engine = not leading. |
| 5 | Fallback | Dev box not leading → each worker runs `cswap auto` on its own with consume-first-reserve-jj. Workers run `cswap auto` permanently. |
| 6 | Manual override | Hosts menu in the dev box TUI. An override lasts until cleared, but only applies while the dev box leads; fallback ignores it. |
| 7 | Reserve account | Workers follow the dev box onto the reserve account too (pure follow). |
| 8 | Settings master | `fleet push` on command; drift is reported, never silently overwritten. |

## Design

### The lease (leader → worker, one ssh round trip per worker per tick)
The dev box's live engine runs a **leader thread** (never on the tick thread: ssh can hang).
Every tick, and immediately after the dev box switches, it runs per worker:

`ssh -o BatchMode=yes -o ConnectTimeout=5 <alias> <cswap-cmd> fleet receive --json` with the
lease on **stdin** (the command line is fixed; no payload is ever interpolated into a shell).

Lease body (`cswap-fleet/1`):
- `leader`: dev box instance id (random, per engine start) and `ttl_s` (default 180).
- `target`: the account the worker should be on: the dev box's active account, or the
  override for this worker (`follow` | `pin N` | `strategy S`).
- `identity`: per slot, a salted hash of (email, organization uuid). The worker refuses to
  follow slot N when its own slot N hashes differently (`identity-mismatch`); slot numbers are
  "the same on every box" by ruling, and this checks it instead of trusting it. No emails sent.
- `settings`: the dev box's effective autoswitch settings the worker applies **live while
  following** (strategy, threshold, model, per-account thresholds/disabled). Not persisted.
- `usage`: the dev box's `list --json` rows, adopted through the existing `import-usage` path
  with `--hold` = ttl, so a following worker stops polling usage.

Expiry is **receipt time + ttl on the worker's clock**: the two clocks never have to agree
(same idea as `usageAgeSeconds`).

### Worker side
- `cswap fleet receive` validates the lease, writes `<backup_root>/fleet/lease.json`
  (atomic), adopts usage, and replies with a **snapshot**: its `list --json` rows, `engine
  status`, follow state (`following` | `overridden` | `autonomous` + why), lease age, settings
  drift hash, and re-login flags. One round trip both pushes and pulls.
- The worker engine (`cswap auto`, permanent) gets one hook at the top of the tick: lease
  valid → target = lease target, switched through the existing `request_switch(target=N)`
  path (freshening, quarantine, `_perform_switch`, the state record: no new credential code);
  lease absent/expired → the normal strategy with `fleet.fallbackStrategy`
  (default `consume-first-reserve-jj`).
- Re-login flag: `usageStatus: relogin_required`, engine quarantine (not exposed today; added
  to the snapshot), and `loginExpiresAt` within 7 days.
- Running permanently: cswap ships an **example** systemd user unit for `cswap auto` in
  `docs/`. Installing it on a worker is the gated "auto-switch on" step; the env repo folds it
  into its worker provisioning.

### Dev box side
- `cswap fleet hosts add|remove|list` → `<backup_root>/fleet/hosts.json` (ssh alias, remote
  cswap command, override). Private by location; nothing host-specific in the repo.
- `cswap fleet status [--json]` → one view of every host (the dev box included): reachable,
  follow state, active account, engine state, per-account usage, re-login flags, drift. With
  no live leader it does a one-shot ssh pull instead. **This is the router contract.**
- `cswap fleet push [--host H] [--dry-run]` → writes the dev box's persisted settings and
  per-account policy (threshold, disabled, priority) to workers through `fleet apply -` on the
  worker, identity-checked per slot. Drift between pushes shows in `fleet status`.
- `cswap fleet override H follow|pin N|strategy S` (CLI twin of the menu).
- `engine status --json` gains an additive `fleet` block (leader on/off, hosts, last push).

### Hosts menu (TUI)
A Hosts screen reachable from the dashboard: one row per worker (state, active account,
lease age, re-login flags, drift), keys to pin to an account, set a strategy, return to
follow, push settings, refresh.

## Contract `cswap-fleet/1` (draft; documented in `docs/fleet.md` when built)
`cswap fleet status --json` → `{"contract": "cswap-fleet/1", "leader": {...}, "hosts":
[{"name", "reachable", "state", "active", "engine": {...}, "accounts": [...], "relogin": [...],
"drift": bool, "lease_age_s"}]}`. Exit codes follow `cswap-engine/0.1`'s table where they
apply (0 ok, 1 error, 2 usage); a host that is unreachable is data (`reachable: false`), not
an exit code. Accounts are slot numbers; no emails in the contract.

## Blast radius
- **Credential paths:** none new. The worker switches through the existing engine switch
  path; nothing moves tokens between boxes (charter out-of-scope). `deep` attacks the follow
  hook anyway, since it is a new reason for the switch path to run.
- **Store layout:** new `<backup_root>/fleet/` dir only (hosts.json on the dev box; lease.json
  + state on workers). No token material in it. Slot dirs untouched, so restic's slot-dir
  discovery and the cross-sessions skill are unaffected; `deep` confirms.
- **Live dev-box behaviour:** with no hosts configured, the leader thread never starts and
  every hook is a pass-through. The merge to `jeremy` is still gated.

## Tests
- Existing contract: `tests/test_engine_control.py`, `tests/test_transfer.py` (import-usage),
  `tests/test_usage_store.py::TestAdopt`, autoswitch manual-pick tests.
- New `tests/test_fleet.py`: lease validate/expire (worker clock), identity mismatch,
  receive→snapshot round trip, push/apply + drift, override persistence, hosts config.
- New engine tests: follows a valid lease, overrides apply only under a lease, falls back to
  consume-first-reserve-jj on expiry and rejoins on the next lease, dry-run engine never leads,
  leader thread survives a hung/failed ssh (fake runner injected; no real ssh in the suite).
- TUI: Hosts screen renders rows and its keys call the override/push functions.
- Trial: `tools/trial/` scratch stores for both roles; then the worker box for real once its
  logins exist (charter permits: test branch, switch its active account, start/stop cswap).

## Rebase cost
New files: `src/claude_swap/fleet.py` (lease, hosts, push, status), `fleet_leader.py` (thread),
`tui/fleetview.py`, `docs/fleet.md`, tests. Upstream-file hunks: `cli.py` (dispatch a `fleet`
verb), `autoswitch.py` (one tick hook + leader start/stop), `engine_control.py` (status block),
`tui/app.py` (one binding), `settings.py` (`fleet.*` keys). Each hunk a few lines.

## Phases
1. **Worker side:** lease, `fleet receive`/`apply`, engine follow hook, fallback, snapshot.
2. **Dev box side:** hosts config, leader thread, `fleet status` (contract), `fleet push`, overrides.
3. **Hosts menu** in the TUI.
4. **Verify:** suite, `review`, `deep` (follow hook + leader thread + store layout), scratch trial.
5. **Gated (Jeremy):** merge to `jeremy`; worker deploy (`git pull`); install + start the
   worker's permanent `cswap auto`; first live follow test. Then the contract version goes to
   the env repo session.

## Not doing
Moving logins between boxes; automating `/login`; the router; worker provisioning; an upstream PR.
