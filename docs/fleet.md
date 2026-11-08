# `cswap fleet`: worker boxes follow the dev box

Fork-only feature. Contract **`cswap-fleet/1`**. Plan: `docs/plans/2026-11-07-fleet-follow.md`.

One box is the **dev box** (the master). Every other box named in its fleet config is a
**worker**. While the dev box runs a live auto-switch engine (`cswap auto`, or the TUI auto
screen), it **leads**: each tick it sends every worker a short-lived lease over ssh, and the
worker's own engine follows it onto the same account (or the worker's override). When the dev
box stops leading, the lease expires and each worker runs its own engine with
`fleet.fallbackStrategy` (default `consume-first-reserve-jj`).

Nothing moves credentials between boxes. Each box keeps its own logins; slot numbers are
expected to mean the same account on every box, and that is **checked** with salted identity
hashes, never trusted. Emails **do** travel inside the ssh payloads between your own boxes
(the lease carries the dev box's `list --json` rows, because importing usage matches accounts
on them; the worker's snapshot reply carries its rows back). They are stripped from everything
printed for a consumer: `fleet status` and `engine status` output carry slot numbers only.

## Setup

On every worker: the fork installed, every account logged in under the same slot numbers, and
`cswap auto` running permanently (see the example unit below).

On the dev box, one ssh alias per worker in `~/.ssh/config` with key auth (cswap always runs
ssh with `BatchMode=yes`, so a password prompt is a failure, never a hang). `hosts add`,
`hosts remove` and `override` (CLI or the TUI Hosts screen) rewrite `hosts.json` under a file
lock, so two of them at once never lose each other's change. Then:

```
cswap fleet hosts add worker-a                       # name defaults to the alias
cswap fleet hosts add worker-b --name lab --command "python3 -m claude_swap"
cswap fleet hosts list
cswap fleet hosts remove lab
```

Hosts live in `<backup_root>/fleet/hosts.json` on the dev box (never in the repo). Validation
at `add`: the alias and name are letters, digits and `._@:-` only, never starting with `-`; the
remote command is split like a shell would (`shlex`) and each word must be a plain word or path
(no quotes, no spaces inside a path, no shell characters). `local` is reserved for the dev box.

## Commands

| Command | Runs on | Does |
|---|---|---|
| `cswap fleet hosts add ALIAS [--name N] [--command C]` | dev box | add a worker |
| `cswap fleet hosts remove NAME` / `hosts list` | dev box | manage workers |
| `cswap fleet status [--json]` | dev box | every host in one view (the contract below) |
| `cswap fleet push [--host NAME] [--dry-run] [--json]` | dev box | write the dev box's settings + per-account policy to workers |
| `cswap fleet override HOST follow\|pin N\|strategy S` | dev box | per-worker override, kept until changed |
| `cswap fleet forget [--json]` | worker | drop the stored lease: a box that left the fleet goes back to its own persisted strategy |
| `cswap fleet alert-test [--json]` | dev box | send one test event through `fleet.alertCommand` (see [Alerts](#alerts)) |
| `cswap fleet receive` | worker | lease on stdin → snapshot on stdout (the leader calls this) |
| `cswap fleet snapshot` | worker | print this box's snapshot |
| `cswap fleet apply [-] [--dry-run]` | worker | settings document on stdin → what changed |

Every verb takes `--json`. `receive`, `snapshot` and `apply` are machine verbs: they always
print exactly one JSON object.

`receive` stores the lease and, when it changes the target, the settings or `follow_blocked`
(or no live lease was stored before), wakes this box's running engine (best effort), so the
worker acts on it within seconds instead of at its next tick; a plain heartbeat does not wake
it. The reply's `lease` block says whether it changed and whether the wake worked
(`"changed"`, `"engine_woken": true|false`).

A worker's `fleet snapshot` and `receive` are store-only: they report the usage already stored
on the box and never fetch; the box's engine is its only usage fetcher.

`forget` removes the stored lease under the lease lock and wakes the engine the same way. With
no lease history the box is no longer a worker: no follow and no `fleet.fallbackStrategy`, just
its own persisted `autoswitch.strategy`. It exits 0 either way; `--json` prints
`{"op": "forget", "result": "forgotten"|"no-lease", "forgot": bool, "engine_woken": bool}`.
If the dev box still lists the host and leads, its next heartbeat makes it a worker again:
remove it there too (`fleet hosts remove`).

### Transport

`ssh -o BatchMode=yes -o ConnectTimeout=5 <alias> <command> fleet <verb> --json`, each argument
shell-quoted for the remote shell. A payload (lease, settings document) travels **only on
stdin** as JSON; nothing from it is ever put on a command line. Timeouts, ssh failures (exit
255), a nonzero remote exit and a reply that is not one JSON object are all reported as errors
for that host; they never stop the other hosts.

### Timing

- **Heartbeat:** the leading dev box sends every worker a fresh lease every
  `min(autoswitch.intervalSeconds, ttl / 3)` seconds (60 s with the defaults), and
  immediately after every switch on the dev box.
- **Lease lifetime:** `ttl_s` is 180 s by default (a worker accepts 30-900 s), counted on the
  **worker's** clock from when it received the lease, so clock skew between boxes does not
  matter.
- **Fallback:** a worker falls back to `fleet.fallbackStrategy` about `ttl_s` after the last
  heartbeat it received (dev box stopped leading, went to sleep, or lost the network).
- **Follow latency:** `fleet receive` wakes the worker's engine, so it follows within seconds
  rather than at its own next tick.
- **ssh:** `ConnectTimeout=5`, and 20 s per call overall. Hosts are contacted in parallel; a
  hung host is skipped for that round and never delays the others.

### Overrides

`follow` (the default) puts the worker on the dev box's active account. `pin N` holds it on
slot N. `strategy S` lets the worker pick with strategy S (one of the selectable strategies).
An override applies **only while the dev box leads**; the fallback ignores it.

### Push and drift

`fleet push` sends a settings document and the worker mirrors it (`fleet apply`):

- every `autoswitch.*` key at the dev box's effective value: a key at its default clears an
  explicit value on the worker, any other value is written. This includes `intervalSeconds`,
  `warmupEnabled` and `warmupStagger` (the dev box is the master);
- every managed slot's policy: per-account `threshold`, `priority` and `disabled`, defaults
  included (so a stray worker override is cleared).

**Never pushed (box-local):** `ui.*` (theme), `run.*` (`cswap run` defaults), `updates.*` (the
update hint), `fleet.*` (each box's own fallback and alert hook), and account aliases. A worker ignores such
keys if a document carries them and lists them under `ignored`.

Per slot, the worker compares a salted hash of its own slot's (email, organization) with the
document's; a slot that differs is skipped (`identity-mismatch`), as is one the worker does not
have (`missing`). Everything is validated before the first write; `--dry-run` reports the
same changes and writes nothing. The key set is exactly what the drift fingerprint hashes, so
after a successful push `fleet status` shows `drift: false`. Drift is only ever reported,
never silently overwritten: pushing is always on command.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `fleet.fallbackStrategy` | `consume-first-reserve-jj` | worker strategy while no lease is valid (only once a lease was ever received; a box with no fleet history behaves exactly as without this feature) |
| `fleet.alertCommand` | (unset: off) | the dev box's needs-login alert hook, a full command line (see [Alerts](#alerts)); box-local |

`cswap config` lists `fleet.fallbackStrategy` on **every** box, fleet or not; it has no effect
until the box has received a lease. `fleet.alertCommand` only matters on the dev box while it
leads.

## Alerts

The leading dev box can tell you when a slot on any box needs a `/login`, or a worker stops
answering, without anyone looking at `fleet status`. cswap only **fires a hook**: it runs a
command you configure and hands it the events as JSON on stdin. Sending the actual message
(mail relay, recipients, format) is the job of your own script.

```
cswap config set fleet.alertCommand '/usr/bin/python3 /opt/alerts/send_alert.py'
cswap config set fleet.alertCommand '"C:\Program Files\Python313\python.exe" "D:\tools\send_alert.py"'
cswap fleet alert-test
cswap config unset fleet.alertCommand        # off
```

The second line is for a POSIX shell or PowerShell 7.3+; Windows PowerShell 5.1 strips the inner
double quotes on the way to a native command, so there write each one as `\"`.

- **The setting** `fleet.alertCommand` is a full command line, never run through a shell. On
  Windows it is split by the Windows rules (double-quote paths with spaces; backslashes are
  literal), elsewhere by POSIX `shlex`. It is box-local: never pushed, never in the drift
  fingerprint. Unset (the default) = no alerts, and nothing is written.
- **Where it runs:** only in the dev box's leader (its live engine with fleet hosts configured),
  at the end of each round. The command runs on its own thread with a **30 s** timeout; at most
  one runs at a time, and it never delays a round or the engine tick. Its stdout and stderr are
  discarded.
- **What is watched:** each worker's last reply (`relogin_detail`) and the dev box itself as
  host `local` (built from its own store, no usage fetch). Worker replies arrive
  asynchronously, so a change on a worker is reported one round (about a minute) after the
  worker reports it.

### Events

| `kind` | When | Fields |
|---|---|---|
| `relogin` | a slot is newly in a host's re-login set, or its `reason` or `expires_at` changed | `host`, `slot`, `reason` (`relogin_required` \| `quarantined` \| `expiring`), `expires_at` (ISO UTC or null) |
| `relogin-cleared` | a slot that was alerted is no longer flagged | `host`, `slot`, `reason` (the alerted one), `since` (when it was alerted) |
| `unreachable` | a worker failed 3 rounds in a row; once per outage | `host`, `since` (first failure) |
| `reachable` | a worker that was alerted unreachable answers again | `host`, `since` (when the outage began) |
| `test` | `cswap fleet alert-test` only | `host` (`local`) |

`reason` is the most urgent one when several apply: `relogin_required` (the store says the login
is gone), then `quarantined` (the engine quarantined the slot), then `expiring` (its login
expires within 7 days, or already has). An unreachable worker's slots are neither alerted nor
cleared until it answers again.

### Payload: contract `cswap-fleet-alert/1`

One JSON object on the command's stdin, holding every event of that round:

```json
{
  "contract": "cswap-fleet-alert/1",
  "generated_at": "2026-11-07T15:00:00Z",
  "events": [
    {"kind": "relogin", "host": "worker-a", "slot": "3", "reason": "expiring",
     "expires_at": "2026-11-12T08:00:00Z"},
    {"kind": "relogin-cleared", "host": "local", "slot": "2", "reason": "relogin_required",
     "since": "2026-11-06T09:00:00Z"},
    {"kind": "unreachable", "host": "worker-b", "since": "2026-11-07T14:57:00Z"}
  ]
}
```

Hosts are the names from `fleet hosts`; the dev box is `local`. Slot numbers are strings. No
emails, organization names or aliases are ever in a payload. Fields may be added within
`cswap-fleet-alert/1`; none will be removed or change meaning.

### Dedup and retry

- What was sent is recorded in `<backup_root>/fleet/alerts.json` (atomic writes, under a file
  lock), per host + slot with its `reason` and `expires_at`, plus the hosts alerted unreachable.
  A restart of the dev box or its engine does not alert again for anything already sent.
- Events are recorded **only after the command exits 0**. A nonzero exit, a timeout or a
  command that cannot start logs a warning (the exit code or failure kind; never the payload)
  and the same events are computed and sent again next round. The warning is logged once per
  failure streak; later failures in the same streak go to the debug log.
- A host removed from `fleet hosts` drops out of `alerts.json` at the next recorded send,
  without a `relogin-cleared` event.

### `cswap fleet alert-test`

Sends one synthetic event (`{"kind": "test", "host": "local"}`) through the configured command,
in the foreground, with the same payload contract and 30 s timeout, and reports the exit code.
Nothing is recorded in `alerts.json`. Use it to check your sender end to end.

| Outcome | Exit | `--json` |
|---|---|---|
| command exited 0 | 0 | `{"op": "alert-test", "result": "sent", "exit_code": 0, "event": {...}}` |
| no `fleet.alertCommand` | 1 | `"error": "no-alert-command"` |
| the command line cannot be split (unbalanced quote) | 1 | `"error": "invalid-alert-command"` |
| nonzero exit, timeout, not found | 1 | `"error": "alert-failed"`, `"exit_code"` (null on timeout / not found) |

## `cswap engine status`: the `fleet` block

`engine status` (contract `cswap-engine/0.1`, unchanged) always carries an additive top-level
`fleet` block, also on a box with no fleet:

```json
"fleet": {"role": "none" | "leader" | "worker", "leader": {...} | null,
          "follow": {...} | null, "refusal": {...} | null}
```

`role: leader` while this box's engine leads; `leader` then carries its per-host snapshots
(what `fleet status` reads). `role: worker` once the box has a stored lease (live or expired);
`follow` is then its follow state (as in a snapshot, including `on_target`) and `refusal` the
target it last refused to follow, if any. `role: none` otherwise: no hosts led and no lease
history (never received one, or `fleet forget`). Like `fleet status`, it carries no emails.

`engine.strategy_source` says where the engine's current strategy came from: `settings`
(the persisted `autoswitch.strategy`), `session` (changed for this run only), `fleet-lease`
(a live lease's strategy override), or `fleet-fallback` (a worker with no valid lease, running
`fleet.fallbackStrategy`).
On a worker, `engine.strategy`, `engine.reserve` and `engine.threshold` are the values its
last tick actually used (the lease's or the fallback's), not the box's own settings.

## Contract `cswap-fleet/1`: `cswap fleet status --json`

```json
{
  "contract": "cswap-fleet/1",
  "generated_at": "2026-11-07T15:00:00Z",
  "leader": {"live": true, "leader_id": "..."},
  "hosts": [
    {
      "name": "local", "alias": null, "local": true, "source": "local",
      "reachable": true, "error": null,
      "state": "leading", "reason": null, "on_target": null, "lease_age_s": null,
      "active": "2",
      "engine": {"running": true, "result": "running", "...": "engine status fields"},
      "accounts": [{"number": "1", "active": false, "usageStatus": "ok", "usage": {}}],
      "relogin": ["3"],
      "relogin_detail": [{"slot": "3", "reason": "expiring", "expires_at": "2026-11-12T08:00:00Z"}],
      "settings_hash": "<sha256>", "drift": false,
      "override": null, "last_ok_at": null
    }
  ]
}
```

Field list (per host; the dev box is always first, named `local`):

| Field | Type | Meaning |
|---|---|---|
| `name`, `alias` | str, str\|null | display name; ssh alias (null for `local`) |
| `local` | bool | the dev box itself |
| `source` | `local`\|`leader`\|`pull` | from the dev box, the live leader's last snapshot, or a one-shot `fleet snapshot` pull |
| `reachable` | bool | false = no usable snapshot; **data, not an exit code** |
| `error` | str\|null | why not reachable (timeout, ssh error, remote exit) |
| `state` | `leading`\|`following`\|`overridden`\|`autonomous`\|`unknown` | `leading` only for `local` with a live leader; `unknown` when unreachable |
| `reason` | str\|null | why autonomous / blocked (e.g. `identity-mismatch`, `lease-expired`) |
| `on_target` | bool\|null | the worker's active slot is its live lease's follow/pin target (from its snapshot's `follow` block). `false` while it has not switched yet, and always under a strategy override (no target); null when unknown (unreachable or an older worker build), and always null for `local` (the dev box follows no one) |
| `lease_age_s` | number\|null | seconds since the worker received its lease (worker clock) |
| `active` | str\|null | active slot number |
| `engine` | object\|null | `running`, `result`, plus the host's `engine status` fields |
| `accounts` | list | per slot: `number`, `active`, `isOrganization`, `usageStatus`, `usage`, `usageFetchedAt`, `usageAgeSeconds`, `lastGood*`, `usageError`, `usageRetryAt`, `disabled`, `threshold`, `loginExpiresAt` (each present when the `list --json` row has it) |
| `relogin` | list[str] | slots needing a re-login: `relogin_required`, engine-quarantined, or login expiring within 7 days |
| `relogin_detail` | list[object] | the same slots with a reason: `{"slot", "reason": "relogin_required"\|"quarantined"\|"expiring", "expires_at": ISO str\|null}`; `[]` from an older worker build |
| `settings_hash` | str\|null | the host's drift fingerprint |
| `drift` | bool\|null | hash differs from the dev box's; null when either hash is unknown |
| `override` | object\|null | `{"mode": "follow"\|"pin"\|"strategy", "account", "strategy"}`; null for `local` |
| `last_ok_at` | ISO str\|null | the leader's last good contact (`source: leader` only) |

**Slot numbers are strings everywhere in the contract** (`active`, `accounts[].number`,
`relogin`, `override.account`), even though `cswap list --json` rows use integers.

**No emails** (nor organization names/uuids or aliases) appear in the contract: account rows
pass through an allowlist and every string is scrubbed. The over-ssh payloads between boxes do
carry the raw `list --json` rows, because importing usage matches accounts on them.

Fields may be added; none will be removed or change meaning within `cswap-fleet/1`.

## Exit codes

Same table as `cswap-engine/0.1` where they apply:

| Code | Meaning |
|---|---|
| 0 | done (`status` with unreachable hosts is still 0) |
| 1 | error: bad hosts file, unknown host, invalid lease/document, no hosts to push to, or a push that failed on any host |
| 2 | usage error |

Errors with `--json` print `{"contract": "cswap-fleet/1", "op", "result": "error", "error":
<code>, "message"}`. Codes: `invalid-host`, `duplicate-host`, `unknown-host`, `invalid-hosts`,
`invalid-override`, `invalid-input`, `invalid-lease`, `invalid-document`, `no-hosts`,
`busy` (`hosts.json` locked by another cswap for over 10 s),
`push-failed` (with per-host results in `hosts`), `no-alert-command`,
`invalid-alert-command`, `alert-failed` (with `exit_code`), `unsupported`, `internal`.

## TUI: the Hosts screen

`h` on the dashboard (only when hosts are configured) opens one row per worker: follow state,
active slot, lease age, re-login slots, drift, and keys for `f` follow / `p` pin / `s` strategy
/ `u` push / `r` refresh over ssh. A worker following (or pinned) but not yet on its target
shows the target next to its active slot, e.g. `active 4 → 2`.

## Running a worker's engine permanently (example)

A worker only follows while its own engine runs, so on a worker `cswap auto` runs
permanently. Below is an **example** systemd user unit; the paths are generic (adjust
`ExecStart` to where cswap is installed on that box). Installing it is the operator's
provisioning step per worker: cswap never installs or enables a unit itself.

```ini
# ~/.config/systemd/user/cswap-auto.service
[Unit]
Description=cswap auto-switch engine (fleet worker)

[Service]
ExecStart=%h/.local/bin/cswap auto
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

`systemctl --user daemon-reload && systemctl --user enable --now cswap-auto`. A user unit
only runs while that user has a session unless lingering is on: it **needs**
`loginctl enable-linger <user>` (once, as root) to start at boot and survive logout.

Leaving the fleet: `fleet hosts remove NAME` on the dev box, then `cswap fleet forget` on the
worker (otherwise it runs `fleet.fallbackStrategy` until forgotten).
