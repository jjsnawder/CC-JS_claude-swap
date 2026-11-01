"""Fork-only: outside control of a running auto-switch engine (`cswap engine`).

Contract ``cswap-engine/0.1`` (docs/plans/2026-11-01-engine-control.md): three
commands -- ``status``, ``pick N``, ``strategy NAME`` -- each printing one JSON
object with ``--json`` and reporting through a fixed set of exit codes.

Two halves, both here so the upstream files carry only call-site hooks:

* :class:`ControlServer` -- attached by an engine HOST (the ``cswap auto``
  loop, or the TUI auto screen) for the engine's lifetime. A
  ``multiprocessing.connection`` Listener (a named pipe on Windows, a Unix
  socket elsewhere) served by a daemon thread, plus a user-only endpoint
  file ``<backup root>/engine-control/<pid>-<token>.json`` holding pid,
  address, authkey, host and started_at. Removed on stop; a file whose pid
  is dead, or whose address no longer answers, counts as no engine.
* the client (:func:`main`), behind the thin ``cswap engine`` dispatch in
  ``cli.py``. It never writes anything.

The authkey authenticates the channel and is never printed, logged, or put
in any payload. No payload carries an email or a token: accounts are numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing import connection as mpc
from pathlib import Path
from typing import Any, Callable

_logger = logging.getLogger(__name__)

CONTRACT = "cswap-engine/0.1"
ENDPOINT_DIRNAME = "engine-control"
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_ENGINE = 3
EXIT_PENDING = 4
EXIT_REFUSED = 5

_ENDPOINT_RE = re.compile(r"^(\d+)-([0-9a-f]+)\.json$")
_EMAIL_RE = re.compile(r"[^\s@<>()\"']+@[^\s@<>()\"']+\.[^\s@<>()\"']+")
# Bounds on the handshake and the request line, so a silent peer can only
# ever stall its own handler thread, never the listener.
_HANDSHAKE_S = 5.0
_REQUEST_WAIT_S = 10.0
_MAX_REQUEST_BYTES = 64 * 1024
# Concurrent handler threads; a connect beyond this is closed at once.
_MAX_HANDLERS = 8
# Listener backlog: 1 makes macOS refuse a second pending connect, and the
# sweep's probe would then delete a live engine's endpoint file.
_BACKLOG = 16
_DECISIVE_KINDS = ("switch", "no-switch", "error", "all-exhausted")
# Stale-file sweep: how long a live-pid file's address gets to answer.
_PROBE_S = 0.5


# ---------------------------------------------------------------------------
# Handshake: mutual HMAC-SHA256 challenge (both ends live in this module)
# ---------------------------------------------------------------------------
#
#   server -> client   _HS_HELLO + nonce_s
#   client -> server   HMAC(key, "client" + nonce_s) + nonce_c
#   server -> client   HMAC(key, "server" + nonce_c)
#
# Every server-side read is preceded by ``conn.poll(_HANDSHAKE_S)``: a silent
# peer costs one handler thread for at most that long, never forever. (The
# default Windows pipe DACL gives other users read access only, not a full
# connect; same-user peers and any reader still meet the bounded wait.) Role labels stop a proof from being
# reflected back at its sender.

_HS_HELLO = b"cswap-engine-hs/1:"
_NONCE_BYTES = 32
_HS_MAX_BYTES = 256


class _HandshakeFailed(Exception):
    pass


def _proof(key: bytes, role: bytes, nonce: bytes) -> bytes:
    return hmac.new(key, _HS_HELLO + role + nonce, hashlib.sha256).digest()


def _recv_within(conn, timeout: float, maxlength: int) -> bytes:
    # poll() bounds the wait for the FIRST byte only. On POSIX a peer that
    # sends a partial frame and stalls blocks recv_bytes() with no timeout.
    # Accepted risk: the socket is user-only, so only the same user (who can
    # already do worse) can pin handler slots this way.
    if not conn.poll(timeout):
        raise _HandshakeFailed("timed out")
    return conn.recv_bytes(maxlength)


def _server_handshake(conn, key: bytes) -> None:
    nonce_s = secrets.token_bytes(_NONCE_BYTES)
    conn.send_bytes(_HS_HELLO + nonce_s)
    msg = _recv_within(conn, _HANDSHAKE_S, _HS_MAX_BYTES)
    proof, nonce_c = msg[:32], msg[32:]
    if len(nonce_c) != _NONCE_BYTES or not hmac.compare_digest(
        proof, _proof(key, b"client", nonce_s)
    ):
        raise _HandshakeFailed("bad proof")
    conn.send_bytes(_proof(key, b"server", nonce_c))


def _client_handshake(conn, key: bytes) -> None:
    hello = _recv_within(conn, _HANDSHAKE_S, _HS_MAX_BYTES)
    nonce_s = hello[len(_HS_HELLO):]
    if not hello.startswith(_HS_HELLO) or len(nonce_s) != _NONCE_BYTES:
        raise _HandshakeFailed("bad hello")
    nonce_c = secrets.token_bytes(_NONCE_BYTES)
    conn.send_bytes(_proof(key, b"client", nonce_s) + nonce_c)
    proof = _recv_within(conn, _HANDSHAKE_S, _HS_MAX_BYTES)
    if not hmac.compare_digest(proof, _proof(key, b"server", nonce_c)):
        raise _HandshakeFailed("bad proof")


def _address_answers(address: Any, timeout: float = _PROBE_S) -> bool:
    """Does anything listen at ``address``? Opens no connection on Windows
    (WaitNamedPipe); a short connect elsewhere. Unsure = True (keep)."""
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.WaitNamedPipe(str(address), int(timeout * 1000))
            return True
        import socket

        family = getattr(socket, mpc.address_type(address))
        with socket.socket(family) as s:
            s.settimeout(timeout)
            s.connect(address)
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except Exception:
        return True


def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _scrub(text: str) -> str:
    """Belt and braces: payload messages never carry an email."""
    return _EMAIL_RE.sub("<email>", text or "")


def _payload(op: str, result: str, **fields) -> dict:
    return {"contract": CONTRACT, "op": op, "result": result, **fields}


def _error(op: str, code: str, message: str, **fields) -> dict:
    return _payload(op, "error", error=code, message=_scrub(message), **fields)


def endpoint_dir(backup_root: Path) -> Path:
    return Path(backup_root) / ENDPOINT_DIRNAME


def _ref_number(ref: dict | None) -> str | None:
    if not isinstance(ref, dict) or ref.get("number") is None:
        return None
    return str(ref["number"])


# ---------------------------------------------------------------------------
# Pick outcome: THE tick that consumed the request
# ---------------------------------------------------------------------------


def pick_reply(engine: Any, outcome: Any, events: list) -> dict:
    """The ``pick`` payload for the tick that consumed a request.

    ``outcome is None`` = the request was replaced (the TUI's own `m`/`n`)
    before any tick consumed it. Otherwise the LAST decisive event of that
    tick decides: switch / no-switch / all-exhausted / error."""
    if outcome is None:
        return _payload(
            "pick",
            "refused",
            reason="superseded",
            message="another switch request replaced this one before it ran",
        )
    decisive = [e for e in events if getattr(e, "kind", "") in _DECISIVE_KINDS]
    if not decisive:
        return _error("pick", "engine-error", "the tick made no switch decision")
    event = decisive[-1]
    if event.kind == "switch":
        src, dst = _ref_number(event.from_ref), _ref_number(event.to_ref)
        dry = bool(event.dry_run)
        pinned = getattr(engine, "pinned", None)
        head = "[dry-run] would switch" if dry else "switched"
        message = f"{head} #{src or '?'} -> #{dst or '?'} (manual pick)"
        if event.note:
            message += f" · {event.note}"
        return _payload(
            "pick",
            "would-switch" if dry else "switched",
            **{
                "from": src,
                "to": dst,
                "pinned": None if pinned is None else str(pinned),
                "dry_run": dry,
                "message": _scrub(message),
            },
        )
    if event.kind == "no-switch":
        return _payload(
            "pick", "refused", reason=event.reason, message=_scrub(event.human())
        )
    if event.kind == "all-exhausted":
        return _payload(
            "pick", "refused", reason="all-exhausted", message=_scrub(event.human())
        )
    return _error("pick", "engine-error", event.message)


class _PickRequest:
    """One delivered pick. Callable: it IS the engine's ``on_result`` hook,
    so the engine can match it by identity (``withdraw_switch``)."""

    def __init__(self, server: "ControlServer", engine: Any, target: str):
        self.server = server
        self.engine = engine
        self.target = target
        self.reply: dict | None = None
        self.done = threading.Event()
        self._once = threading.Lock()

    def __call__(self, outcome, events) -> None:  # engine thread
        try:
            # The CURRENT engine's pin: a TUI rebuild may have retired the
            # engine that ran the tick (the pin carries over to the new one).
            current = self.server._current()
            engine = current if current is not None else self.engine
            reply = pick_reply(engine, outcome, events)
        except Exception as e:  # never leave _inflight stuck (every call: busy)
            reply = _error("pick", "engine-error", type(e).__name__)
        self.finish(reply)

    def finish(self, reply: dict) -> None:
        with self._once:
            if self.done.is_set():
                return
            self.reply = reply
            self.done.set()
        self.server._release(self)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class ControlServer:
    """Listener thread + endpoint file for one engine host.

    ``engine`` is a zero-arg callable returning the host's CURRENT engine:
    the TUI rebuilds its engine on `s`/`l`, so the server outlives any one
    of them. ``apply_strategy(name)`` applies a strategy live (the TUI's
    rebuild path, or ``AutoSwitchEngine.apply_strategy``)."""

    def __init__(
        self,
        engine: Callable[[], Any],
        *,
        host: str,
        backup_root: Path,
        apply_strategy: Callable[[str], None] | None = None,
    ):
        self._engine = engine
        self.host = host
        self.backup_root = Path(backup_root)
        self._apply_strategy = apply_strategy or (
            lambda name: self._engine().apply_strategy(name)
        )
        self._authkey = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._inflight: _PickRequest | None = None
        self._strategy_busy = False
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(_MAX_HANDLERS)
        self._listener: mpc.Listener | None = None
        self._thread: threading.Thread | None = None
        self.endpoint_path: Path | None = None
        self.started_at: str | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        from claude_swap.settings import atomic_write_json

        self._sweep_stale()
        # No authkey on the Listener itself: its built-in handshake runs
        # INSIDE accept(), where one silent client would block every other.
        # Each handler thread runs this module's bounded handshake instead.
        self._listener = mpc.Listener(backlog=_BACKLOG)
        self.started_at = _iso(time.time())
        directory = endpoint_dir(self.backup_root)
        self.endpoint_path = directory / f"{os.getpid()}-{secrets.token_hex(4)}.json"
        atomic_write_json(
            self.endpoint_path,
            {
                "contract": CONTRACT,
                "pid": os.getpid(),
                "address": self._listener.address,
                "authkey": self._authkey.hex(),
                "host": self.host,
                "started_at": self.started_at,
            },
        )
        self._thread = threading.Thread(
            target=self._accept_loop, name="cswap-engine-control", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        if self.endpoint_path is not None:
            try:
                self.endpoint_path.unlink()
            except OSError:
                pass
        self._reap()  # an unconsumed pick dies with the engine: say so
        listener = self._listener
        if listener is not None:
            try:  # unblock accept(): the loop sees _stopping and exits
                mpc.Client(listener.address).close()
            except Exception:
                pass
            if self._thread is not None:
                self._thread.join(timeout=2.0)
            try:
                listener.close()
            except Exception:
                pass

    def _sweep_stale(self) -> None:
        from claude_swap.process_detection import is_pid_alive

        directory = endpoint_dir(self.backup_root)
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            m = _ENDPOINT_RE.fullmatch(path.name)
            if not m or path == self.endpoint_path:
                continue
            pid = int(m.group(1))
            if pid == os.getpid() or is_pid_alive(pid):
                # Windows: a pid can read alive while a handle pins an exited
                # process. Its address is the tiebreak: silent = stale.
                try:
                    address = json.loads(path.read_text(encoding="utf-8"))["address"]
                except Exception:
                    continue  # unreadable: not ours to judge
                if _address_answers(address):
                    continue
            try:
                path.unlink()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                conn = self._listener.accept()
            except Exception as e:
                if self._stopping.is_set():
                    return
                failures += 1
                _logger.debug("engine-control accept failed: %s", type(e).__name__)
                if failures > 20:
                    return
                time.sleep(0.1)
                continue
            failures = 0
            if self._stopping.is_set():
                conn.close()
                return
            if not self._slots.acquire(blocking=False):
                _close_quietly(conn)  # every handler busy: shed, keep accepting
                continue
            try:
                threading.Thread(
                    target=self._serve, args=(conn,), name="cswap-engine-request",
                    daemon=True,
                ).start()
            except Exception as e:
                self._slots.release()
                _close_quietly(conn)
                _logger.debug("engine-control handler failed: %s", type(e).__name__)

    def _serve(self, conn) -> None:
        """Handler thread; owns one ``_slots`` permit, released on exit."""
        try:
            try:
                _server_handshake(conn, self._authkey)
            except Exception:
                return  # wrong key, silent, or gone: say nothing, close
            try:
                raw = _recv_within(conn, _REQUEST_WAIT_S, _MAX_REQUEST_BYTES)
            except _HandshakeFailed:
                return
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError
            except Exception:
                reply = _error("?", "bad-request", "request is not a JSON object")
            else:
                reply = self.handle(request)
            conn.send_bytes(json.dumps(reply).encode("utf-8"))
        except Exception as e:
            _logger.debug("engine-control request failed: %s", type(e).__name__)
        finally:
            _close_quietly(conn)
            self._slots.release()

    # -- requests -----------------------------------------------------------

    def handle(self, request: dict) -> dict:
        """One request -> one contract payload (transport-free, for tests)."""
        op = request.get("op")
        try:
            if op == "status":
                return self._status()
            if op == "pick":
                return self._pick(
                    str(request.get("target", "")),
                    float(request.get("timeout", DEFAULT_TIMEOUT)),
                )
            if op == "strategy":
                return self._strategy(str(request.get("strategy", "")))
        except Exception as e:  # never let a request kill the host
            return _error(str(op), "engine-error", type(e).__name__)
        return _error(str(op), "bad-request", f"unknown op {op!r}")

    def _current(self) -> Any:
        try:
            return self._engine()
        except Exception:
            return None

    def _release(self, request: _PickRequest) -> None:
        with self._lock:
            if self._inflight is request:
                self._inflight = None

    def _reap(self) -> None:
        """Retire a pick whose engine is gone (host stopping, or the TUI
        rebuilt its engine). Withdrawn = it can never run: answer it.
        Already consumed = that tick is still running and will answer."""
        with self._lock:
            request = self._inflight
        if request is None:
            return
        if not self._stopping.is_set() and self._current() is request.engine:
            return
        withdraw = getattr(request.engine, "withdraw_switch", None)
        if withdraw is not None and withdraw(request):
            request.finish(
                _error("pick", "engine-stopped", "the engine stopped before the pick ran")
            )

    def _status(self) -> dict:
        from claude_swap.autoswitch import reserve_accounts
        from claude_swap.settings import load_settings

        self._reap()
        engine = self._current()
        if engine is None:
            return _error("status", "engine-error", "engine not started")
        settings = engine.settings
        try:
            configured = load_settings(self.backup_root).strategy
        except Exception:
            configured = None
        try:
            active = engine.switcher.current_account_number()
        except Exception:
            active = None
        pinned = getattr(engine, "pinned", None)
        with self._lock:
            pick_pending = self._inflight is not None
        return _payload(
            "status",
            "running",
            engine={
                "pid": os.getpid(),
                "host": self.host,
                "mode": "dry-run" if engine.dry_run else "live",
                "strategy": settings.strategy,
                "strategy_source": (
                    "settings" if settings.strategy == configured else "session"
                ),
                "reserve": sorted(reserve_accounts(settings), key=int),
                "threshold": float(settings.threshold),
                "active": None if active is None else str(active),
                "pinned": None if pinned is None else str(pinned),
                "pick_pending": pick_pending,
                "started_at": self.started_at,
                "last_tick_at": _iso(getattr(engine, "last_tick_at", None)),
            },
        )

    def _pick(self, target: str, timeout: float) -> dict:
        if not target.isdigit():
            return _error("pick", "bad-request", "account must be a number")
        if not math.isfinite(timeout):  # NaN would never reach the deadline
            timeout = DEFAULT_TIMEOUT
        timeout = min(max(timeout, 0.0), MAX_TIMEOUT)
        self._reap()
        engine = self._current()
        if engine is None or self._stopping.is_set():
            return _error("pick", "engine-error", "engine not started")
        with self._lock:
            if self._inflight is not None or self._strategy_busy:
                return _payload(
                    "pick",
                    "refused",
                    reason="busy",
                    message="another engine request is still pending",
                )
            request = _PickRequest(self, engine, target)
            self._inflight = request
        try:
            engine.request_switch(target=target, on_result=request)
        except Exception as e:
            self._release(request)
            return _error("pick", "engine-error", type(e).__name__)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if request.done.wait(max(0.0, min(0.25, remaining))):
                return request.reply
            if remaining <= 0:
                if request.done.is_set():  # landed between wait and here
                    return request.reply
                # Still in flight: the request stays registered (busy, and
                # `pick_pending` in status) until its tick answers.
                return _payload(
                    "pick",
                    "pending",
                    message=(
                        f"no outcome within {timeout:g}s; the pick may still "
                        "land -- check `cswap engine status`"
                    ),
                )
            self._reap()

    def _strategy(self, name: str) -> dict:
        from claude_swap.settings import SETTING_SPECS, set_setting

        choices = SETTING_SPECS["autoswitch.strategy"].choices
        if name not in choices:
            return _error("strategy", "bad-request", f"unknown strategy {name!r}")
        self._reap()
        if self._current() is None or self._stopping.is_set():
            return _error("strategy", "engine-error", "engine not started")
        with self._lock:
            if self._inflight is not None or self._strategy_busy:
                return _error(
                    "strategy",
                    "busy",
                    "another engine request is still pending",
                    persisted=False,
                    live=False,
                )
            self._strategy_busy = True
        try:
            try:
                set_setting(self.backup_root, "autoswitch.strategy", name)
            except Exception as e:
                return _error(
                    "strategy",
                    "settings-error",
                    f"could not persist: {type(e).__name__}",
                    persisted=False,
                    live=False,
                )
            try:
                self._apply_strategy(name)
            except Exception as e:
                return _error(
                    "strategy",
                    "engine-error",
                    f"persisted, but not applied live: {type(e).__name__}",
                    persisted=True,
                    live=False,
                )
            return _payload(
                "strategy", "applied", strategy=name, persisted=True, live=True
            )
        finally:
            with self._lock:
                self._strategy_busy = False


def start_server(
    engine: Callable[[], Any],
    *,
    host: str,
    backup_root: Path,
    apply_strategy: Callable[[str], None] | None = None,
) -> ControlServer | None:
    """Host hook: start a control channel, or None. Never raises -- a
    control channel that cannot start must never cost the engine."""
    server = ControlServer(
        engine, host=host, backup_root=backup_root, apply_strategy=apply_strategy
    )
    try:
        server.start()
    except Exception as e:
        _logger.debug("engine-control unavailable: %s", type(e).__name__)
        try:
            server.stop()
        except Exception:
            pass
        return None
    return server


def stop_server(server: ControlServer | None) -> None:
    """Host hook: stop and remove the endpoint. Never raises."""
    if server is None:
        return
    try:
        server.stop()
    except Exception as e:  # pragma: no cover - safety net
        _logger.debug("engine-control stop failed: %s", type(e).__name__)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    pid: int
    address: str
    authkey: bytes
    host: str

    def __repr__(self) -> str:  # never let the key reach a log or traceback
        return f"Endpoint(pid={self.pid}, host={self.host!r})"


def discover(backup_root: Path) -> list[Endpoint]:
    """Endpoint files whose pid is alive. Reads only; never deletes."""
    from claude_swap.process_detection import is_pid_alive

    directory = endpoint_dir(backup_root)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.iterdir()):
        if not _ENDPOINT_RE.fullmatch(path.name):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ep = Endpoint(
                pid=int(data["pid"]),
                address=str(data["address"]),
                authkey=bytes.fromhex(data["authkey"]),
                host=str(data.get("host", "")),
            )
        except Exception:
            continue
        if is_pid_alive(ep.pid):
            found.append(ep)
    return found


class _Unreachable(Exception):
    """A live engine is there but could not be reached (counts as one)."""


class _AuthFailed(_Unreachable):
    pass


def _connect(ep: Endpoint):
    """Authenticated connection, or None when nothing listens (stale).

    Only "no such endpoint" / "refused" mean stale. Any other OSError (e.g.
    a Windows pipe-busy timeout) is a live engine we could not reach: raising
    keeps the reply a ``transport`` error, never ``no-engine``, so the hub
    does not fall back to a competing switch."""
    try:
        conn = mpc.Client(ep.address)
    except (FileNotFoundError, ConnectionRefusedError, ValueError):
        return None
    except OSError:
        raise _Unreachable from None
    try:
        _client_handshake(conn, ep.authkey)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise _AuthFailed from None
    return conn


def request(backup_root: Path, message: dict, *, reply_timeout: float) -> dict:
    """Deliver ``message`` to the one running engine; the contract payload."""
    op = str(message.get("op"))
    conns, unreachable_engines = [], 0
    for ep in discover(backup_root):
        try:
            conn = _connect(ep)
        except _Unreachable:
            unreachable_engines += 1
            continue
        if conn is not None:
            conns.append(conn)
    try:
        if len(conns) + unreachable_engines > 1:
            return _error(
                op,
                "multiple-engines",
                "more than one auto-switch engine is running "
                "(cswap auto and/or TUI auto screens); stop all but one",
            )
        if not conns:
            if unreachable_engines:
                return _error(op, "transport", "an engine is running but did not answer")
            return _payload(op, "no-engine")
        conn = conns[0]
        try:
            conn.send_bytes(json.dumps(message).encode("utf-8"))
            if not conn.poll(reply_timeout):
                if op == "pick":
                    return _payload(
                        "pick",
                        "pending",
                        message="no reply from the engine in time; check "
                        "`cswap engine status`",
                    )
                return _error(op, "transport", "no reply from the engine in time")
            reply = json.loads(conn.recv_bytes().decode("utf-8"))
        except (EOFError, OSError, ValueError) as e:
            return _error(op, "transport", f"engine connection failed ({type(e).__name__})")
        if not isinstance(reply, dict):
            return _error(op, "transport", "malformed reply from the engine")
        return reply
    finally:
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass


def exit_code(payload: dict) -> int:
    result = payload.get("result")
    if result in ("running", "switched", "would-switch", "applied"):
        return EXIT_OK
    if result == "no-engine":
        return EXIT_NO_ENGINE
    if result == "pending":
        return EXIT_PENDING
    if result == "refused":
        return EXIT_REFUSED
    return EXIT_ERROR


def human_line(payload: dict) -> str:
    result = payload.get("result")
    if result == "no-engine":
        return "no auto-switch engine is running"
    if result == "error":
        return f"error ({payload.get('error')}): {payload.get('message', '')}"
    op = payload.get("op")
    if op == "status":
        e = payload.get("engine") or {}
        reserve = e.get("reserve") or []
        parts = [
            f"engine running: {e.get('host')} ({e.get('mode')}, pid {e.get('pid')})",
            f"strategy {e.get('strategy')} ({e.get('strategy_source')})",
        ]
        if reserve:
            parts.append("reserve " + ", ".join(f"#{n}" for n in reserve))
        parts.append(f"threshold {e.get('threshold'):g}%")
        parts.append(f"active #{e.get('active')}" if e.get("active") else "no active account")
        if e.get("pinned"):
            parts.append(f"holding #{e.get('pinned')}")
        if e.get("pick_pending"):
            parts.append("pick pending")
        return " · ".join(parts)
    if op == "pick":
        if result == "refused":
            return f"refused ({payload.get('reason')}): {payload.get('message', '')}"
        return str(payload.get("message", result))
    if op == "strategy":
        return f"strategy set to {payload.get('strategy')} (saved to settings, applied live)"
    return str(result)


def _account_number(value: str) -> str:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"invalid account number: {value!r}")
    return str(int(value))


def _timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid timeout: {value!r}") from None
    if not 0 < seconds <= MAX_TIMEOUT:
        raise argparse.ArgumentTypeError(f"timeout must be in (0, {MAX_TIMEOUT:g}]")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    from claude_swap.settings import SETTING_SPECS

    parser = argparse.ArgumentParser(
        prog="cswap engine",
        description=(
            "Control a running auto-switch engine (`cswap auto` or a TUI auto "
            f"screen) from outside. Contract {CONTRACT}."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  done (status: running; pick: switched or dry-run preview; strategy: applied)
  1  error (multiple-engines, transport, engine-error, ...)
  2  usage error
  3  no engine running: nothing was done
  4  pending: pick delivered, no outcome within --timeout (check status)
  5  refused by the engine (pick only)
        """,
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON object")
    sub = parser.add_subparsers(dest="action", metavar="{status,pick,strategy}")
    sub.required = True
    p_status = sub.add_parser("status", help="Report the running engine")
    p_pick = sub.add_parser("pick", help="Switch to account N through the engine")
    p_pick.add_argument("account", type=_account_number, metavar="N")
    p_pick.add_argument(
        "--timeout",
        type=_timeout,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"Wait this long for the outcome (default {DEFAULT_TIMEOUT:g})",
    )
    p_strategy = sub.add_parser(
        "strategy", help="Persist a strategy and apply it to the running engine"
    )
    p_strategy.add_argument(
        "name", choices=SETTING_SPECS["autoswitch.strategy"].choices
    )
    for p in (p_status, p_pick, p_strategy):
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Print one JSON object",
        )
    return parser


def run(argv: list[str], *, backup_root: Path | None = None) -> int:
    """Parse, deliver, print; returns the exit code (argparse exits 2)."""
    args = build_parser().parse_args(argv)
    if backup_root is None:
        from claude_swap.paths import get_backup_root

        backup_root = get_backup_root()
    if args.action == "status":
        message, wait = {"op": "status"}, _REQUEST_WAIT_S + 10.0
    elif args.action == "pick":
        message = {"op": "pick", "target": args.account, "timeout": args.timeout}
        wait = args.timeout + 15.0
    else:
        message, wait = {"op": "strategy", "strategy": args.name}, 60.0
    payload = request(backup_root, message, reply_timeout=wait)
    if args.json:
        print(json.dumps(payload), flush=True)
    else:
        print(human_line(payload), flush=True)
    return exit_code(payload)


def main(argv: list[str]) -> None:
    """``cswap engine ...`` entry (the cli.py dispatch)."""
    sys.exit(run(argv))
