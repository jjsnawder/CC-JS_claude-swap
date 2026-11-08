"""Fork (fleet): the two roles a running auto-switch engine can play.

* **Leader** (dev box, live engine only): :class:`FleetLeader`, a daemon
  thread that pushes a lease to every configured worker over ssh
  (``fleet receive``) on an interval and right after every live switch, and
  keeps each worker's last reply for ``snapshot()``.
* **Worker** (any box that has ever received a lease): :class:`EngineFleet`
  gives the engine its per-tick view of the stored lease -- the follow/pin
  target, the strategy override and the leader's live settings -- plus the
  fallback strategy once the lease is gone.

``autoswitch.py`` only calls in through :class:`EngineFleet` (``# fork:
fleet`` hunks). A box with no ``<backup_root>/fleet/`` files never calls
into ``fleet_lease``/``fleet_remote`` (only the file-name constants are
imported) and its engine behaves exactly as upstream: every hook is a
pass-through.

Follow semantics (see ``docs/plans/2026-11-07-fleet-follow.md``):

* off the target → the tick is a manual pick of the target, the same path
  ``request_switch(target=N)`` takes (freshen, quarantine, ``_perform``,
  state record), so ``m``'s refusals apply. A refusal is kept (reported in
  ``engine status`` → ``fleet.refusal``) and the pick is not retried for
  ``FOLLOW_RETRY_S`` unless the lease target changes; meanwhile the box
  holds below its line and escapes over it like any engine.
* on the target → the engine's own strategy never moves it: below the line
  and over it (``proactive``) it holds, since every box shares the account
  and the leader moves them all. Only ``at-limit`` and ``failover`` escape;
  the escape is recorded like a refusal so the box does not bounce back.
* a follow switch is not a held pick: it never sets ``pinned``, and it is
  exempt from the cooldown exactly as ``m`` is.
* while following, this box spawns no warmup hellos (the leader warms the
  shared accounts); fallback warms per the box's own settings.
"""

from __future__ import annotations

import importlib
import logging
import math
import queue
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claude_swap.fleet_lease import FLEET_DIRNAME, HOSTS_FILENAME, LEASE_FILENAME

_logger = logging.getLogger("claude-swap")

DEFAULT_FALLBACK_STRATEGY = "consume-first-reserve-jj"
MAX_WORKERS = 4
REMOTE_TIMEOUT_S = 20.0
RECEIVE_ARGS = ("fleet", "receive", "--json")
# How long a refused (or escaped-from) follow target is left alone before the
# next attempt, unless the lease names a different target first.
FOLLOW_RETRY_S = 300.0
FOLLOW_MODES = ("follow", "pin")
# Refusals that clear on their own within a tick or two (a hello in flight,
# an unreadable usage row, nothing to compare against): retried next tick,
# never parked for FOLLOW_RETRY_S.
TRANSIENT_REFUSALS = frozenset({"target-busy", "target-usage-unknown", "no-comparison"})


def _lease_mod():
    # import_module, not `from claude_swap import ...`: tests swap the module
    # in sys.modules, and the package attribute would bypass that.
    return importlib.import_module("claude_swap.fleet_lease")


def _remote_mod():
    return importlib.import_module("claude_swap.fleet_remote")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def has_lease_history(backup_root: Path) -> bool:
    """Whether this box has ever received a lease (the fallback trigger)."""
    return (Path(backup_root) / FLEET_DIRNAME / LEASE_FILENAME).exists()


def has_hosts_file(backup_root: Path) -> bool:
    return (Path(backup_root) / FLEET_DIRNAME / HOSTS_FILENAME).exists()


def fallback_strategy(backup_root: Path) -> str:
    """``fleet.fallbackStrategy`` (settings.py's loader validates it);
    ``consume-first-reserve-jj`` if the loader is unavailable or fails."""
    try:
        from claude_swap.settings import load_fleet_settings

        return load_fleet_settings(Path(backup_root)).fallback_strategy
    except Exception as e:
        _logger.debug("fleet: fallbackStrategy unreadable: %r", e)
        return DEFAULT_FALLBACK_STRATEGY


# -- the settings a lease carries ------------------------------------------------


def live_settings(engine: Any) -> dict:
    """The leader engine's effective settings, as the lease's ``settings``.

    ``engine.settings`` is the LIVE snapshot (session threshold and strategy
    included); per-account lines and the disabled set come from the store."""
    s = engine.settings
    switcher = engine.switcher
    try:
        policies = switcher.account_policies()
    except Exception:
        policies = {}
    try:
        disabled = set(switcher.disabled_account_numbers())
    except Exception:
        disabled = set()
    accounts = {
        str(num): {
            "threshold": policy.threshold,
            "priority": policy.priority,
            "disabled": str(num) in disabled,
        }
        for num, policy in policies.items()
    }
    return {
        "strategy": s.strategy,
        "threshold": s.threshold,
        "weeklyThreshold": s.weekly_threshold,
        "model": s.model,
        "reserveAccounts": s.reserve_accounts,
        "accounts": accounts,
    }


def _bounded(value: Any, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(min(max(value, lo), hi))


def apply_lease_settings(
    base: Any, doc: Any, *, mismatched: Any = ()
) -> tuple[Any, dict, frozenset]:
    """``base`` with the lease's live settings applied (never persisted).

    Returns ``(settings, per-account thresholds, leader-disabled slots)``.
    Every field is validated on its own; a bad or missing one keeps the
    box's value, so a malformed lease degrades to local settings.
    ``mismatched``: slots whose leader-side account is a different one here
    (``Lease.identity_mismatch``); the leader's per-slot policy skips them."""
    from claude_swap.settings import (
        AUTOSWITCH_STRATEGIES,
        SETTING_SPECS,
        parse_account_numbers,
    )

    if not isinstance(doc, dict):
        return base, {}, frozenset()
    changes: dict = {}
    if doc.get("strategy") in AUTOSWITCH_STRATEGIES:
        changes["strategy"] = doc["strategy"]
    spec = SETTING_SPECS["autoswitch.threshold"]
    if (t := _bounded(doc.get("threshold"), spec.lo, spec.hi)) is not None:
        changes["threshold"] = t
    if "weeklyThreshold" in doc:
        raw = doc["weeklyThreshold"]
        spec = SETTING_SPECS["autoswitch.weeklyThreshold"]
        if raw is None:
            changes["weekly_threshold"] = None
        elif (w := _bounded(raw, spec.lo, spec.hi)) is not None:
            changes["weekly_threshold"] = w
    if "model" in doc:
        raw = doc["model"]
        if raw is None or (isinstance(raw, str) and raw):
            changes["model"] = raw
    if "reserveAccounts" in doc:
        raw = doc["reserveAccounts"]
        if isinstance(raw, str) and (raw == "none" or parse_account_numbers(raw)):
            changes["reserve_accounts"] = (
                raw if raw == "none" else ",".join(parse_account_numbers(raw))
            )
    thresholds: dict[str, float | None] = {}
    disabled: set[str] = set()
    accounts = doc.get("accounts")
    skip = {str(n) for n in mismatched or ()}
    if isinstance(accounts, dict):
        for num, entry in accounts.items():
            if not (isinstance(num, str) and num.isascii() and num.isdigit()):
                continue
            if num in skip:
                continue
            if not isinstance(entry, dict):
                continue
            if "threshold" in entry:
                raw = entry["threshold"]
                if raw is None:
                    thresholds[num] = None
                elif (t := _bounded(raw, 1.0, 100.0)) is not None:
                    thresholds[num] = t
            if entry.get("disabled") is True:
                disabled.add(num)
    settings = replace(base, **changes) if changes else base
    return settings, thresholds, frozenset(disabled)


# -- worker: this tick's view of the lease --------------------------------------


@dataclass(frozen=True)
class WorkerTick:
    """``mode``: follow | pin | strategy (a valid lease) or fallback (none,
    expired, or ``follow_blocked``)."""

    mode: str
    target: str | None
    settings: Any
    thresholds: dict
    disabled: frozenset
    lease: Any = None

    @property
    def following(self) -> bool:
        return self.mode in FOLLOW_MODES and self.target is not None


def worker_tick(backup_root: Path, base: Any, *, now: float) -> WorkerTick | None:
    """None when this box has no lease history: the engine is untouched."""
    root = Path(backup_root)
    if not has_lease_history(root):
        return None
    try:
        lease = _lease_mod().active_lease(root, now=now)
    except Exception as e:  # never let the fleet cost a tick
        _logger.debug("fleet: active_lease failed: %r", e)
        lease = None
    if lease is None or getattr(lease, "follow_blocked", None):
        return WorkerTick(
            "fallback",
            None,
            replace(base, strategy=fallback_strategy(root)),
            {},
            frozenset(),
            lease,
        )
    settings, thresholds, disabled = apply_lease_settings(
        base, lease.settings, mismatched=getattr(lease, "identity_mismatch", ())
    )
    target = None
    mode = lease.target_mode
    if mode in FOLLOW_MODES:
        acct = lease.target_account
        target = str(acct) if acct is not None and str(acct).isdigit() else None
    elif mode == "strategy":
        from claude_swap.settings import AUTOSWITCH_STRATEGIES

        if lease.target_strategy in AUTOSWITCH_STRATEGIES:
            settings = replace(settings, strategy=lease.target_strategy)
    return WorkerTick(mode, target, settings, thresholds, disabled, lease)


class EngineFleet:
    """Per-engine fleet state; every method is safe on a box with no fleet."""

    def __init__(self, engine: Any):
        self.engine = engine
        self.tick: WorkerTick | None = None
        self.following = False  # this tick's manual target came from the lease
        self._attempt: str | None = None
        self.refusal: dict | None = None
        self._models_touched = False
        self.leader: FleetLeader | None = None
        self._leader_lock = threading.Lock()
        self.in_loop = False
        self._leader_failed_logged = False

    # -- worker hooks (engine thread) ----------------------------------------

    def begin_tick(self, base: Any) -> Any:
        """The settings this tick decides with (``base`` when no fleet)."""
        self.following = False
        self._attempt = None
        engine = self.engine
        tick = None
        root = engine.switcher.backup_dir
        # The stat comes first: a box with no fleet history must not even
        # read the clock (engine tests pin exact clock() call sequences).
        if has_lease_history(root):
            try:
                tick = worker_tick(root, base, now=engine.clock())
            except Exception as e:  # pragma: no cover - safety net
                _logger.debug("fleet: worker tick failed: %r", e)
        self.tick = tick
        if tick is None:
            settings = base
        else:
            settings = tick.settings
            if self.refusal is not None and self.refusal["target"] != tick.target:
                self.refusal = None  # the leader moved: try the new target now
        if tick is not None or self._models_touched:
            # The model axes are engine state fixed at construction; a lease
            # may name others for as long as it holds.
            from claude_swap.settings import parse_model_names

            want = parse_model_names(settings.model)
            if want != engine._models:
                engine._models = want
                engine._model_check_done = not want
                self._models_touched = True
        return settings

    def policies(self, policies: dict) -> dict:
        tick = self.tick
        if tick is None or not tick.thresholds:
            return policies
        out = dict(policies)
        for num, threshold in tick.thresholds.items():
            if num in out:
                out[num] = replace(out[num], threshold=threshold)
        return out

    def switchable(self, numbers: list) -> list:
        tick = self.tick
        if tick is None or not tick.disabled:
            return numbers
        return [n for n in numbers if n not in tick.disabled]

    def follow_target(self, current: str) -> str | None:
        """The account this tick should manually pick, or None."""
        tick = self.tick
        if tick is None or not tick.following or tick.target == current:
            return None
        r = self.refusal
        if r is not None and r["target"] == tick.target and self.engine.clock() < r["retry_at"]:
            return None
        self.following = True
        self._attempt = tick.target
        if self.engine._tick_events is None:
            self.engine._tick_events = []  # to read the refusal at tick end
        return tick.target

    def hold_detail(self, current: str, *, over: bool) -> str | None:
        """Why the engine's own strategy must not move off ``current`` now
        (None = let it decide). ``over``: the active is over its line but not
        at its limit (``proactive``); at-limit and failover never get here."""
        tick = self.tick
        if tick is None or not tick.following:
            return None
        if current == tick.target:
            return f"on Account-{current}, following the fleet leader"
        if over:
            return None  # off target and over the line: escape as usual
        r = self.refusal or {}
        return (
            f"waiting to follow Account-{tick.target}"
            + (f" ({r.get('reason')})" if r.get("reason") else "")
        )

    def cap_delay(self, delay: float, interval: float) -> float:
        """Backstop for a missed wake: while a valid, unblocked lease holds,
        the engine never sleeps past ``intervalSeconds`` (a BLOCKED or idle
        crawl would otherwise sit out a leader's move for many minutes)."""
        tick = self.tick
        if tick is None or tick.mode == "fallback":
            return delay
        return min(delay, max(float(interval), 1.0))

    def warmup_suppressed(self) -> bool:
        """Following: the leader's engine warms the shared accounts."""
        tick = self.tick
        return tick is not None and tick.following

    def event_trigger(self, trigger: str) -> str:
        return "fleet" if self.following else trigger

    def after_switch(self, from_number: Any, trigger: str = "") -> str:
        """Live switch done; returns a note for the SwitchEvent ("" = none)."""
        note = ""
        tick = self.tick
        if self.following:
            self.engine._pinned = None  # a follow is not a held pick
            self.refusal = None
            note = "following the fleet leader"
        elif (
            trigger in ("at-limit", "failover")
            and tick is not None
            and tick.following
            and str(from_number) == tick.target
        ):
            # An escape off the target: don't bounce straight back onto it.
            now = self.engine.clock()
            self.refusal = {
                "target": tick.target,
                "reason": "escaped",
                "detail": f"left Account-{tick.target} at its limit or unreadable",
                "at": now,
                "retry_at": now + FOLLOW_RETRY_S,
            }
        self.kick()
        return note

    def end_tick(self, outcome: Any) -> None:
        if self.following and self._attempt is not None:
            from claude_swap.autoswitch import ErrorEvent, NoSwitchEvent, TickOutcome

            if outcome is TickOutcome.SWITCHED:
                if self.engine.dry_run:
                    self.refusal = None
            else:
                events = list(self.engine._tick_events or ())
                last = next(
                    (e for e in reversed(events) if isinstance(e, (NoSwitchEvent, ErrorEvent))),
                    None,
                )
                now = self.engine.clock()
                if isinstance(last, NoSwitchEvent):
                    reason, detail = last.reason, last.detail
                else:
                    reason = "error"
                    detail = getattr(last, "message", "") if last is not None else ""
                transient = (
                    outcome in (TickOutcome.ERROR, TickOutcome.BLOCKED)
                    or reason in TRANSIENT_REFUSALS
                )
                self.refusal = {
                    "target": self._attempt,
                    "reason": reason,
                    "detail": detail,
                    "at": now,
                    # A transient refusal retries next tick; a real one waits.
                    "retry_at": now if transient else now + FOLLOW_RETRY_S,
                }
        self.following = False
        self._attempt = None
        if self.in_loop:
            self.ensure_leader()  # never raises

    def refusal_public(self) -> dict | None:
        r = self.refusal
        if r is None:
            return None
        return {
            "target": r["target"],
            "reason": r["reason"],
            "detail": r.get("detail") or "",
            "retry_at": _iso(r.get("retry_at")),
        }

    # -- leader lifecycle ----------------------------------------------------

    def ensure_leader(self) -> None:
        """Start the leader if this engine is live and hosts are configured.
        Never raises: a leader that cannot start must not cost a tick."""
        try:
            self._ensure_leader()
        except Exception as e:
            log = _logger.debug if self._leader_failed_logged else _logger.warning
            self._leader_failed_logged = True
            log("fleet: leader start failed: %s", type(e).__name__)

    def _ensure_leader(self) -> None:
        engine = self.engine
        if engine.dry_run or engine._stop.is_set() or self.leader is not None:
            return
        root = engine.switcher.backup_dir
        if not has_hosts_file(root):
            return
        try:
            hosts = _remote_mod().load_hosts(root)
        except Exception as e:
            _logger.debug("fleet: load_hosts failed: %r", e)
            return
        if not hosts:
            return
        with self._leader_lock:
            if self.leader is not None or engine._stop.is_set():
                return
            ttl = float(getattr(_lease_mod(), "DEFAULT_TTL_S", 180))
            leader = FleetLeader(
                engine.switcher,
                engine,
                root,
                interval_s=min(float(engine.settings.interval_seconds), ttl / 3),
            )
            leader.start()
            self.leader = leader

    def stop_leader(self) -> None:
        with self._leader_lock:
            leader, self.leader = self.leader, None
        if leader is not None:
            leader.stop()

    def kick(self) -> None:
        leader = self.leader
        if leader is not None:
            leader.kick()


# -- engine status `fleet` block --------------------------------------------------


def effective_strategy(engine: Any) -> tuple[str, str] | None:
    """``(strategy, source)`` when the fleet hook decided the engine's last
    tick (``fleet-lease`` | ``fleet-fallback``); None = the engine's own."""
    fleet = getattr(engine, "_fleet", None)
    tick = fleet.tick if isinstance(fleet, EngineFleet) else None
    if tick is None:
        return None
    strategy = getattr(tick.settings, "strategy", None)
    if not isinstance(strategy, str):
        return None
    return strategy, ("fleet-fallback" if tick.mode == "fallback" else "fleet-lease")


def _scrub_tree(value: Any) -> Any:
    from claude_swap.engine_control import _scrub

    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list):
        return [_scrub_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_tree(v) for k, v in value.items()}
    return value


def _public_host_snapshot(snap: Any) -> dict | None:
    """A worker's reply, fit for ``engine status``: its ``list --json`` rows
    go through ``fleet_remote._account_rows`` (no email), every other string
    through the email scrub. Same shape, so ``fleet_status`` reads it as is."""
    if not isinstance(snap, dict):
        return None
    accounts = snap.get("accounts")
    accounts_out: dict = {"accounts": _remote_mod()._account_rows(accounts)}
    if isinstance(accounts, dict):
        for key in ("schemaVersion", "activeAccountNumber", "error"):
            if key in accounts:
                accounts_out[key] = accounts[key]
    out = {k: snap[k] for k in (
        "contract", "host_time", "engine", "follow", "relogin", "settings_hash", "lease",
    ) if k in snap}
    out["accounts"] = accounts_out
    return _scrub_tree(out)


def public_leader_snapshot(leader: Any) -> dict:
    """``leader.snapshot()`` with each host's reply made email-free."""
    snap = leader.snapshot()
    hosts = {
        name: {**entry, "snapshot": _public_host_snapshot(entry.get("snapshot"))}
        for name, entry in (snap.get("hosts") or {}).items()
    }
    return _scrub_tree({**snap, "hosts": hosts})


def status_block(engine: Any, backup_root: Path, reply: dict | None) -> dict:
    """``{"role", "leader", "follow", "refusal"}`` for ``engine status``.
    Never carries an email: worker replies are scrubbed here."""
    fleet = getattr(engine, "_fleet", None)
    leader = getattr(fleet, "leader", None)
    refusal = fleet.refusal_public() if isinstance(fleet, EngineFleet) else None
    if leader is not None and leader.running:
        return {"role": "leader", "leader": public_leader_snapshot(leader),
                "follow": None, "refusal": None}
    if has_lease_history(backup_root):
        eng = reply.get("engine") if isinstance(reply, dict) else None
        active = eng.get("active") if isinstance(eng, dict) else None
        try:
            follow = _lease_mod().follow_state(Path(backup_root), reply, active=active)
        except Exception as e:
            _logger.debug("fleet: follow_state failed: %r", e)
            follow = None
        return {"role": "worker", "leader": None, "follow": follow, "refusal": refusal}
    return {"role": "none", "leader": None, "follow": None, "refusal": None}


# -- leader ---------------------------------------------------------------------


def host_target(host: Any, active: str | None) -> dict:
    """The lease target for one worker: its override, ``follow`` → active."""
    override = getattr(host, "override", None) or {}
    mode = override.get("mode", "follow")
    if mode == "pin" and override.get("account") is not None:
        return {"mode": "pin", "account": str(override["account"]), "strategy": None}
    if mode == "strategy" and override.get("strategy"):
        return {"mode": "strategy", "account": None, "strategy": override["strategy"]}
    return {"mode": "follow", "account": None if active is None else str(active),
            "strategy": None}


class FleetLeader:
    """Pushes leases to every configured worker from a daemon thread.

    ``engine`` is the leader's engine, or a zero-arg callable returning it.
    Per-host ssh calls run on at most ``MAX_WORKERS`` daemon threads; a host
    with a call still in flight is skipped that round, so one hung host
    never delays another, the next round, or the engine tick."""

    def __init__(
        self,
        switcher: Any,
        engine: Any,
        backup_root: Path,
        *,
        runner: Callable[..., Any] = subprocess.run,
        interval_s: float = 60.0,
        clock: Callable[[], float] = time.time,
        max_workers: int = MAX_WORKERS,
        timeout_s: float = REMOTE_TIMEOUT_S,
    ):
        self.switcher = switcher
        self._engine = engine
        self.backup_root = Path(backup_root)
        self.runner = runner
        self.interval_s = max(float(interval_s), 1.0)
        self.clock = clock
        self.timeout_s = timeout_s
        self.leader_id = secrets.token_hex(8)
        self._seq = 0
        self._max_workers = max(1, min(int(max_workers), MAX_WORKERS))
        self._lock = threading.Lock()
        self._hosts: dict[str, dict] = {}
        self._inflight: set[str] = set()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    def engine(self) -> Any:
        e = self._engine
        return e() if callable(e) and not hasattr(e, "settings") else e

    def start(self) -> None:
        if self._thread is not None:
            return
        for i in range(self._max_workers):
            t = threading.Thread(
                target=self._work, name=f"cswap-fleet-host-{i}", daemon=True
            )
            t.start()
            self._workers.append(t)
        self._thread = threading.Thread(
            target=self._loop, name="cswap-fleet-leader", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Never blocks: in-flight ssh calls finish (bounded by their timeout)
        on daemon threads and are then discarded."""
        self._stop.set()
        self._kick.set()
        for _ in self._workers:
            self._queue.put(None)

    def kick(self) -> None:
        """Push now (after a switch) instead of at the next interval."""
        self._kick.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._kick.clear()
            try:
                self.run_round()
            except Exception as e:  # never let a round kill the leader
                _logger.debug("fleet leader round failed: %r", e)
            self._kick.wait(self.interval_s)

    def run_round(self) -> int:
        """Queue one lease per configured host; returns how many were queued."""
        hosts = _remote_mod().load_hosts(self.backup_root)
        engine = self.engine()
        try:
            active = engine.switcher.current_account_number()
        except Exception:
            active = None
        settings_doc = live_settings(engine)
        lease_mod = _lease_mod()
        self._seq += 1
        queued = 0
        usage: dict | None = None  # one store-only list per round, built lazily
        names = {h.name for h in hosts}
        with self._lock:
            for gone in set(self._hosts) - names:
                self._hosts.pop(gone, None)
        for host in hosts:
            with self._lock:
                if host.name in self._inflight:
                    continue  # still waiting on the last one: never pile up
            try:
                if usage is None:
                    usage = lease_mod.list_payload(self.switcher)
                lease = lease_mod.build_lease(
                    self.switcher,
                    leader_id=self.leader_id,
                    seq=self._seq,
                    target=host_target(host, active),
                    settings=settings_doc,
                    ttl_s=lease_mod.DEFAULT_TTL_S,
                    usage=usage,
                )
            except Exception as e:
                self._record(host.name, None, f"could not build lease ({type(e).__name__})")
                continue
            with self._lock:
                self._inflight.add(host.name)
            self._queue.put((host, lease))
            queued += 1
        return queued

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            host, lease = item
            try:
                if self._stop.is_set():
                    continue
                try:
                    res = _remote_mod().run_remote(
                        host, list(RECEIVE_ARGS), lease,
                        timeout=self.timeout_s, runner=self.runner,
                    )
                    payload, error = res.payload, res.error
                except Exception as e:  # run_remote never raises; belt and braces
                    payload, error = None, f"ssh failed ({type(e).__name__})"
                if not self._stop.is_set():
                    self._record(host.name, payload if error is None else None, error)
            finally:
                with self._lock:
                    self._inflight.discard(host.name)

    def _record(self, name: str, payload: dict | None, error: str | None) -> None:
        ok = error is None and isinstance(payload, dict)
        with self._lock:
            prev = self._hosts.get(name) or {}
            self._hosts[name] = {
                "reachable": ok,
                "last_ok_at": self.clock() if ok else prev.get("last_ok_at"),
                "error": None if ok else (error or "no reply"),
                # The last good reply survives a failure (status shows it
                # with reachable: false).
                "snapshot": payload if ok else prev.get("snapshot"),
            }

    def snapshot(self) -> dict:
        with self._lock:
            hosts = {
                name: {
                    "reachable": entry["reachable"],
                    "last_ok_at": _iso(entry["last_ok_at"]),
                    "error": entry["error"],
                    "snapshot": entry["snapshot"],
                }
                for name, entry in self._hosts.items()
            }
        return {"leader_id": self.leader_id, "hosts": hosts}
