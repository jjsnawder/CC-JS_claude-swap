"""Fork (fleet): needs-login alerts through a hook. Contract ``cswap-fleet-alert/1``.

The leader (the dev box's live engine) calls :meth:`AlertDispatcher.submit`
at the end of each round with what it saw: each host's re-login slots, the
hosts down for :data:`UNREACHABLE_ROUNDS` rounds in a row and the hosts that
answered. Events are computed against ``<backup_root>/fleet/alerts.json``
(what was already sent) and handed to ``fleet.alertCommand`` as one JSON
document on stdin, from a daemon thread with a :data:`ALERT_TIMEOUT_S`
timeout. At most one command runs at a time; an event is recorded as sent
only after the command exits 0, so a failure is simply recomputed (and
retried) next round. The command is never run through a shell.

cswap only fires the hook: the sender (relay, recipients, format) is the
operator's. Payloads carry host names and slot numbers, never an email.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claude_swap.fleet_lease import FLEET_DIRNAME

ALERT_CONTRACT = "cswap-fleet-alert/1"
ALERTS_FILENAME = "alerts.json"
ALERT_TIMEOUT_S = 30.0
UNREACHABLE_ROUNDS = 3
LOCAL_HOST = "local"  # == fleet_remote.LOCAL_HOST_NAME
EVENT_KINDS = ("relogin", "relogin-cleared", "unreachable", "reachable", "test")

_logger = logging.getLogger("claude-swap")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# -- the command line ---------------------------------------------------------


def _split_windows(command: str) -> list[str]:
    """Split like ``CommandLineToArgvW`` / the MS C runtime: the program
    name ends at its closing quote (backslashes are literal there); in the
    other words a backslash is literal unless a run of them precedes a
    quote (2n → n and a delimiter, 2n+1 → n and a literal quote), and ``""``
    inside quotes is a literal quote. An unterminated quote is an error."""
    ws = " \t"
    n = len(command)
    i = 0
    while i < n and command[i] in ws:
        i += 1
    if i >= n:
        return []
    args: list[str] = []
    if command[i] == '"':
        j = command.find('"', i + 1)
        if j < 0:
            raise ValueError("unterminated quote")
        args.append(command[i + 1:j])
        i = j + 1
        if i < n and command[i] not in ws:
            raise ValueError("text directly after the quoted program name")
    else:
        j = i
        while j < n and command[j] not in ws:
            j += 1
        args.append(command[i:j])
        i = j
    while True:
        while i < n and command[i] in ws:
            i += 1
        if i >= n:
            return args
        buf: list[str] = []
        quoted = False
        while i < n:
            c = command[i]
            if c == "\\":
                k = i
                while k < n and command[k] == "\\":
                    k += 1
                run = k - i
                if k < n and command[k] == '"':
                    buf.append("\\" * (run // 2))
                    if run % 2:
                        buf.append('"')
                        i = k + 1
                    else:
                        i = k  # the quote is a delimiter: next pass
                else:
                    buf.append("\\" * run)
                    i = k
                continue
            if c == '"':
                if quoted and i + 1 < n and command[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                quoted = not quoted
                i += 1
                continue
            if c in ws and not quoted:
                break
            buf.append(c)
            i += 1
        if quoted:
            raise ValueError("unterminated quote")
        args.append("".join(buf))


def split_command(command: str, *, windows: bool | None = None) -> list[str]:
    """``fleet.alertCommand`` → argv. Windows rules on Windows (quoted paths
    with spaces and backslashes), POSIX ``shlex`` elsewhere. Raises
    ValueError on an unbalanced quote; empty → ``[]``."""
    if not isinstance(command, str) or not command.strip():
        return []
    if windows is None:
        windows = sys.platform == "win32"
    return _split_windows(command) if windows else shlex.split(command)


def alert_command(backup_root: Path) -> str:
    """The configured command (``""`` = off, or unreadable)."""
    try:
        from claude_swap.settings import load_fleet_settings

        return load_fleet_settings(Path(backup_root)).alert_command or ""
    except Exception as e:
        _logger.debug("fleet: alertCommand unreadable: %r", e)
        return ""


def run_alert(
    argv: list[str],
    payload: dict,
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = ALERT_TIMEOUT_S,
) -> tuple[int | None, str | None]:
    """Run the hook once with ``payload`` on stdin; never raises.

    Returns ``(exit code or None, error or None)``. Its stdout/stderr are
    captured and dropped: nothing the sender prints reaches a log."""
    kwargs: dict = {
        "input": json.dumps(payload),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "shell": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = runner(list(argv), **kwargs)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout:g}s"
    except FileNotFoundError:
        return None, "command not found"
    except Exception as e:
        return None, f"failed to start ({type(e).__name__})"
    rc = getattr(proc, "returncode", None)
    if rc == 0:
        return 0, None
    return rc, f"exit {rc}"


def build_payload(events: list[dict], *, now: float | None = None) -> dict:
    return {
        "contract": ALERT_CONTRACT,
        "generated_at": _iso(time.time() if now is None else now),
        "events": [dict(e) for e in events],
    }


# -- dedup state --------------------------------------------------------------


def alerts_path(backup_root: Path) -> Path:
    return Path(backup_root) / FLEET_DIRNAME / ALERTS_FILENAME


def _empty_state() -> dict:
    return {"contract": ALERT_CONTRACT, "relogin": {}, "unreachable": {}}


def load_state(backup_root: Path) -> dict:
    """``{"relogin": {host: {slot: {"reason", "expires_at", "since"}}},
    "unreachable": {host: {"since"}}}``: what was sent and still holds.
    A missing or malformed file is empty state (worst case: a repeat)."""
    try:
        raw = json.loads(alerts_path(backup_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    state = _empty_state()
    if not isinstance(raw, dict):
        return state
    relogin = raw.get("relogin")
    if isinstance(relogin, dict):
        for host, slots in relogin.items():
            if isinstance(host, str) and isinstance(slots, dict):
                kept = {
                    str(slot): {
                        "reason": entry.get("reason"),
                        "expires_at": entry.get("expires_at"),
                        "since": entry.get("since"),
                    }
                    for slot, entry in slots.items()
                    if isinstance(entry, dict)
                }
                if kept:
                    state["relogin"][host] = kept
    down = raw.get("unreachable")
    if isinstance(down, dict):
        for host, entry in down.items():
            if isinstance(host, str):
                since = entry.get("since") if isinstance(entry, dict) else None
                state["unreachable"][host] = {"since": since}
    return state


def save_state(backup_root: Path, state: dict) -> None:
    from claude_swap.settings import atomic_write_json

    atomic_write_json(alerts_path(backup_root), state)


# -- events -------------------------------------------------------------------


def normalize_detail(value: Any) -> list[dict] | None:
    """A snapshot's ``relogin_detail``, type-checked (None if not a list)."""
    from claude_swap.fleet_lease import RELOGIN_REASONS

    if not isinstance(value, list):
        return None
    out = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("slot")
        slot = str(slot) if isinstance(slot, (str, int)) and not isinstance(slot, bool) else ""
        reason = entry.get("reason")
        expires = entry.get("expires_at")
        if not (slot.isascii() and slot.isdigit()) or slot in seen:
            continue
        if reason not in RELOGIN_REASONS:
            continue
        seen.add(slot)
        out.append({
            "slot": slot,
            "reason": reason,
            "expires_at": expires if isinstance(expires, str) and len(expires) <= 40 else None,
        })
    return out


def detail_from_snapshot(snapshot: Any, *, now: float | None = None) -> list[dict] | None:
    """A host's re-login slots with reasons, from its snapshot.

    Uses ``relogin_detail``; an older worker build sends only ``relogin``,
    so the reasons are then derived from its own account rows (a slot the
    rows do not explain is engine-quarantined). None = nothing usable."""
    if not isinstance(snapshot, dict):
        return None
    accounts = snapshot.get("accounts")
    if isinstance(accounts, dict) and accounts.get("error"):
        return None  # the worker's list failed: its set is unknown, not empty
    detail = normalize_detail(snapshot.get("relogin_detail"))
    if detail is not None:
        return detail
    slots = snapshot.get("relogin")
    if not isinstance(slots, list):
        return None
    from claude_swap.fleet_lease import relogin_detail

    host_time = snapshot.get("host_time")
    if isinstance(host_time, bool) or not isinstance(host_time, (int, float)) or host_time <= 0:
        host_time = time.time() if now is None else now
    wanted = {str(s) for s in slots if str(s).isascii() and str(s).isdigit()}
    derived = relogin_detail(snapshot.get("accounts"), None, now=float(host_time),
                             quarantined=set())
    by_slot = {d["slot"]: d for d in derived if d["slot"] in wanted}
    for slot in wanted - set(by_slot):
        by_slot[slot] = {"slot": slot, "reason": "quarantined", "expires_at": None}
    return [by_slot[s] for s in sorted(by_slot, key=int)]


def compute_events(
    state: dict,
    *,
    relogin: dict[str, list[dict]],
    down: dict[str, str | None],
    up: set[str] | frozenset[str],
) -> list[dict]:
    """Events not yet sent, given this round's view.

    ``relogin``: host → its current re-login detail (only hosts with a fresh
    snapshot; an unreachable host's slots are neither alerted nor cleared).
    ``down``: host → outage start, for hosts failing
    :data:`UNREACHABLE_ROUNDS` rounds in a row. ``up``: hosts that answered."""
    events: list[dict] = []
    sent_down = state.get("unreachable") or {}
    for host in sorted(down):
        if host not in sent_down:
            events.append({"kind": "unreachable", "host": host, "since": down[host]})
    for host in sorted(up):
        if host in sent_down and host not in down:
            events.append({"kind": "reachable", "host": host,
                           "since": (sent_down[host] or {}).get("since")})
    sent = state.get("relogin") or {}
    for host in sorted(relogin):
        current = {d["slot"]: d for d in relogin[host]}
        before = sent.get(host) or {}
        for slot in sorted(current, key=int):
            d = current[slot]
            prev = before.get(slot)
            if prev is None or (prev.get("reason"), prev.get("expires_at")) != (
                d["reason"], d["expires_at"]
            ):
                events.append({"kind": "relogin", "host": host, "slot": slot,
                               "reason": d["reason"], "expires_at": d["expires_at"]})
        for slot in sorted((s for s in before if s not in current),
                           key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else 0, s)):
            prev = before[slot]
            events.append({"kind": "relogin-cleared", "host": host, "slot": slot,
                           "reason": prev.get("reason"), "since": prev.get("since")})
    return events


def apply_events(
    state: dict,
    events: list[dict],
    *,
    at: str | None,
    known_hosts: set[str] | frozenset[str] | None = None,
) -> dict:
    """``state`` with ``events`` recorded as sent (a new dict). Hosts no
    longer configured are dropped (``known_hosts``; None = keep all)."""
    relogin = {h: dict(slots) for h, slots in (state.get("relogin") or {}).items()}
    down = dict(state.get("unreachable") or {})
    for e in events:
        kind, host = e.get("kind"), e.get("host")
        if kind == "relogin":
            relogin.setdefault(host, {})[e["slot"]] = {
                "reason": e.get("reason"), "expires_at": e.get("expires_at"), "since": at,
            }
        elif kind == "relogin-cleared":
            relogin.get(host, {}).pop(e["slot"], None)
        elif kind == "unreachable":
            down[host] = {"since": e.get("since") or at}
        elif kind == "reachable":
            down.pop(host, None)
    if known_hosts is not None:
        relogin = {h: s for h, s in relogin.items() if h in known_hosts}
        down = {h: v for h, v in down.items() if h in known_hosts}
    relogin = {h: s for h, s in relogin.items() if s}
    return {"contract": ALERT_CONTRACT, "relogin": relogin, "unreachable": down}


# -- dispatch -----------------------------------------------------------------


class AlertDispatcher:
    """The leader's alert sender: one command in flight at most, run on a
    daemon thread, recorded in ``alerts.json`` only after exit 0."""

    def __init__(
        self,
        backup_root: Path,
        *,
        runner: Callable[..., Any] = subprocess.run,
        timeout_s: float = ALERT_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
    ):
        self.backup_root = Path(backup_root)
        self.runner = runner
        self.timeout_s = timeout_s
        self.clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failing = False  # throttles the failure warning to once per streak
        self._bad_command_logged = False
        self.last_result: tuple[int | None, str | None] | None = None

    @property
    def in_flight(self) -> bool:
        with self._lock:
            return self._thread is not None

    def command(self) -> str:
        return alert_command(self.backup_root)

    def submit(
        self,
        *,
        relogin: dict[str, list[dict]],
        down: dict[str, str | None],
        up: set[str],
        known_hosts: set[str],
        command: str | None = None,
    ) -> bool:
        """Compute events and start the command if any; True if started.
        Never blocks on the command and never raises."""
        try:
            return self._submit(relogin, down, up, known_hosts, command)
        except Exception as e:
            _logger.debug("fleet: alert round failed: %r", e)
            return False

    def _submit(self, relogin, down, up, known_hosts, command) -> bool:
        command = self.command() if command is None else command
        if not command:
            return False
        try:
            argv = split_command(command)
        except ValueError:
            argv = []
        if not argv:
            if not self._bad_command_logged:
                self._bad_command_logged = True
                _logger.warning("fleet: fleet.alertCommand cannot be split into a command line")
            return False
        self._bad_command_logged = False
        with self._lock:
            if self._thread is not None:
                return False  # one in flight: this round's view is recomputed next round
        events = compute_events(load_state(self.backup_root),
                                relogin=relogin, down=down, up=up)
        if not events:
            return False
        payload = build_payload(events, now=self.clock())
        with self._lock:
            if self._thread is not None:
                return False
            t = threading.Thread(
                target=self._send,
                args=(argv, payload, frozenset(known_hosts)),
                name="cswap-fleet-alert",
                daemon=True,
            )
            self._thread = t
        t.start()
        return True

    def _send(self, argv: list[str], payload: dict, known_hosts: frozenset) -> None:
        try:
            rc, error = run_alert(argv, payload, runner=self.runner, timeout=self.timeout_s)
            self.last_result = (rc, error)
            if error is None:
                self._record(payload, known_hosts)
                if self._failing:
                    _logger.info("fleet: alert command succeeded again")
                self._failing = False
            else:
                log = _logger.debug if self._failing else _logger.warning
                self._failing = True
                log("fleet: alert command failed (%s); retrying next round", error)
        except Exception as e:
            _logger.warning("fleet: alert send failed (%s)", type(e).__name__)
        finally:
            with self._lock:
                self._thread = None

    def _record(self, payload: dict, known_hosts: frozenset) -> None:
        from claude_swap.locking import FileLock

        lock = FileLock(alerts_path(self.backup_root).parent / ".alerts.lock")
        with lock:
            state = load_state(self.backup_root)
            state = apply_events(state, payload["events"], at=payload["generated_at"],
                                 known_hosts=known_hosts)
            save_state(self.backup_root, state)

    def join(self, timeout: float | None = None) -> None:
        """Tests: wait for the in-flight send (if any)."""
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout)


def send_test(
    backup_root: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = ALERT_TIMEOUT_S,
) -> dict:
    """``cswap fleet alert-test``: one synthetic ``test`` event, run in the
    foreground. ``{"command": bool, "valid": bool, "exit_code", "error",
    "payload"}``; nothing is recorded in ``alerts.json``."""
    command = alert_command(backup_root)
    if not command:
        return {"command": False, "valid": False, "exit_code": None,
                "error": "no-alert-command", "payload": None}
    try:
        argv = split_command(command)
    except ValueError as e:
        return {"command": True, "valid": False, "exit_code": None,
                "error": f"cannot split fleet.alertCommand ({e})", "payload": None}
    if not argv:
        return {"command": True, "valid": False, "exit_code": None,
                "error": "fleet.alertCommand is empty after splitting", "payload": None}
    payload = build_payload([{"kind": "test", "host": LOCAL_HOST}])
    rc, error = run_alert(argv, payload, runner=runner, timeout=timeout)
    return {"command": True, "valid": True, "exit_code": rc, "error": error,
            "payload": payload}
