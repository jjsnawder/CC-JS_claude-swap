"""`cswap engine` (fork: engine_control) -- contract cswap-engine/0.1.

Every server here is a REAL ``multiprocessing.connection`` Listener in this
process, with its endpoint file under a tmp backup root; the client is the
real ``engine_control.run`` the CLI dispatches to. Engines are a minimal fake
(contract shapes, timing) or the real ``AutoSwitchEngine`` on the
``EngineHarness`` store (correlation through the actual tick).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap import cli, engine_control
from claude_swap.autoswitch import (
    NoSwitchEvent,
    PollEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.engine_control import CONTRACT
from claude_swap.settings import AutoSwitchSettings

from tests.test_autoswitch import EngineHarness, _usage7, harness  # noqa: F401

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeEngine:
    """The surface the server reads: settings, dry_run, pinned, switcher,
    last_tick_at, request_switch(on_result=), withdraw_switch, apply_strategy.

    ``respond(target) -> (outcome, events, pinned)`` answers a pick from a
    separate thread (as the engine's tick would); None = never answers."""

    def __init__(self, *, dry_run=False, strategy="best", reserve="1",
                 pinned=None, active="4", respond=None):
        self.settings = AutoSwitchSettings(strategy=strategy, reserve_accounts=reserve)
        self.dry_run = dry_run
        self.pinned = pinned
        self.active = active
        self.switcher = SimpleNamespace(current_account_number=lambda: self.active)
        self.last_tick_at = None
        self.respond = respond
        self.requests: list[tuple[str | None, object]] = []
        self.applied: list[str] = []
        self._pending = None
        self._lock = threading.Lock()

    def request_switch(self, target=None, *, on_result=None):
        with self._lock:
            superseded, self._pending = self._pending, on_result
        self.requests.append((target, on_result))
        if superseded is not None:
            superseded(None, [])
        if self.respond is not None and on_result is not None:
            def tick():
                time.sleep(0.05)
                with self._lock:
                    if self._pending is not on_result:
                        return
                    self._pending = None
                outcome, events, pinned = self.respond(target)
                self.pinned = pinned
                self.last_tick_at = time.time()
                on_result(outcome, events)
            threading.Thread(target=tick, daemon=True).start()

    def withdraw_switch(self, on_result):
        with self._lock:
            if self._pending is not on_result:
                return False
            self._pending = None
        return True

    def consume(self):
        """Simulate a tick consuming the pending request (no answer yet)."""
        with self._lock:
            cb, self._pending = self._pending, None
        return cb

    def apply_strategy(self, name):
        self.settings = dataclasses.replace(self.settings, strategy=name)
        self.applied.append(name)


def _ref(n):
    return {"number": int(n), "email": f"user{n}@example.com"}


def _switched(frm, to, *, dry=False, note=""):
    return [
        PollEvent(active=_ref(frm), headroom={}, threshold=90.0),
        SwitchEvent(trigger="manual", from_ref=_ref(frm), to_ref=_ref(to),
                    dry_run=dry, note=note),
    ]


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "backup"
    r.mkdir()
    return r


@pytest.fixture
def servers():
    started: list = []
    yield started
    for s in started:
        engine_control.stop_server(s)


def _serve(root, servers, engine, *, host="auto", apply_strategy=None):
    getter = engine if callable(engine) else (lambda: engine)
    server = engine_control.start_server(
        getter, host=host, backup_root=root, apply_strategy=apply_strategy,
    )
    assert server is not None
    servers.append(server)
    return server


def _cli(root, *argv, capsys):
    """Run the client the way `cswap engine ... --json` does."""
    code = engine_control.run(list(argv), backup_root=root)
    out = capsys.readouterr()
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    assert len(lines) == 1, out.out  # exactly one JSON object
    return code, json.loads(lines[0]), out


def _tree(path: Path) -> dict:
    return {
        str(p.relative_to(path)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(path.rglob("*"))
    }


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_running_shape_exit_0(self, root, servers, capsys):
        (root / "settings.json").write_text(json.dumps(
            {"schemaVersion": 1, "autoswitch": {"strategy": "consume-first-reserve-jj"}}
        ))
        engine = FakeEngine(strategy="consume-first-reserve-jj", reserve="1")
        engine.last_tick_at = 1_790_000_000.0
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "status", "--json", capsys=capsys)
        assert code == 0
        assert payload["contract"] == CONTRACT
        assert payload["op"] == "status" and payload["result"] == "running"
        e = payload["engine"]
        assert set(e) == {
            "pid", "host", "mode", "strategy", "strategy_source", "reserve",
            "threshold", "active", "pinned", "pick_pending", "started_at",
            "last_tick_at",
        }
        assert e["pid"] == os.getpid()
        assert e["host"] == "auto" and e["mode"] == "live"
        assert e["strategy"] == "consume-first-reserve-jj"
        assert e["strategy_source"] == "settings"
        assert e["reserve"] == ["1"]
        assert e["threshold"] == 90.0 and isinstance(e["threshold"], float)
        assert e["active"] == "4" and e["pinned"] is None
        assert e["pick_pending"] is False
        assert e["started_at"].endswith("Z")
        assert e["last_tick_at"].endswith("Z")

    @pytest.mark.parametrize(
        "strategy,reserve,expected",
        [
            ("consume-first-reserve-jj", "1,4", ["1", "4"]),
            ("consume-first-reserve-jj", "none", []),
            ("best", "1", []),
            ("consume-first", "1,4", []),
        ],
    )
    def test_reserve_is_number_strings_empty_unless_reserve_strategy(
        self, root, servers, capsys, strategy, reserve, expected
    ):
        _serve(root, servers, FakeEngine(strategy=strategy, reserve=reserve))
        _, payload, _ = _cli(root, "status", "--json", capsys=capsys)
        assert payload["engine"]["reserve"] == expected

    def test_session_override_and_dry_run_tui(self, root, servers, capsys):
        # settings say best (default); the engine runs consume-first = session
        engine = FakeEngine(dry_run=True, strategy="consume-first", pinned="2")
        _serve(root, servers, engine, host="tui")
        _, payload, _ = _cli(root, "status", "--json", capsys=capsys)
        e = payload["engine"]
        assert e["host"] == "tui" and e["mode"] == "dry-run"
        assert e["strategy_source"] == "session"
        assert e["pinned"] == "2"
        assert e["last_tick_at"] is None

    def test_no_engine_exit_3_and_writes_nothing(self, root, capsys):
        before = _tree(root)
        code, payload, _ = _cli(root, "status", "--json", capsys=capsys)
        assert code == 3
        assert payload == {"contract": CONTRACT, "op": "status", "result": "no-engine"}
        assert _tree(root) == before
        assert not (root / engine_control.ENDPOINT_DIRNAME).exists()

    def test_human_line_without_json(self, root, servers, capsys):
        _serve(root, servers, FakeEngine())
        code = engine_control.run(["status"], backup_root=root)
        out = capsys.readouterr().out.strip()
        assert code == 0
        assert out.startswith("engine running: auto (live")
        assert not out.startswith("{")


# ---------------------------------------------------------------------------
# endpoint file, stale files, multiple engines, authkey hygiene
# ---------------------------------------------------------------------------


class TestEndpoint:
    def test_file_contents_and_removed_on_stop(self, root):
        server = engine_control.start_server(
            lambda: FakeEngine(), host="auto", backup_root=root
        )
        path = server.endpoint_path
        assert path.parent == root / engine_control.ENDPOINT_DIRNAME
        data = json.loads(path.read_text())
        assert set(data) == {"contract", "pid", "address", "authkey", "host", "started_at"}
        assert data["pid"] == os.getpid() and data["host"] == "auto"
        assert len(bytes.fromhex(data["authkey"])) == 32
        if sys.platform != "win32":
            assert (path.stat().st_mode & 0o777) == 0o600
        engine_control.stop_server(server)
        assert not path.exists()

    def test_stale_file_dead_pid_is_no_engine_and_untouched(self, root, capsys):
        directory = root / engine_control.ENDPOINT_DIRNAME
        directory.mkdir()
        stale = directory / "999999-deadbeef.json"
        stale.write_text(json.dumps({
            "pid": 999999, "address": r"\\.\pipe\nothing-here" if sys.platform == "win32"
            else "/nonexistent/sock", "authkey": "00" * 32, "host": "auto",
            "started_at": "2026-11-01T00:00:00Z",
        }))
        before = _tree(root)
        with patch("claude_swap.process_detection.is_pid_alive", return_value=False):
            code, payload, _ = _cli(root, "status", "--json", capsys=capsys)
        assert code == 3 and payload["result"] == "no-engine"
        assert _tree(root) == before  # the client never deletes

    def test_stale_file_live_pid_dead_address_is_no_engine(self, root, capsys):
        directory = root / engine_control.ENDPOINT_DIRNAME
        directory.mkdir()
        (directory / f"{os.getpid()}-0badc0de.json").write_text(json.dumps({
            "pid": os.getpid(),
            "address": r"\\.\pipe\cswap-test-gone" if sys.platform == "win32"
            else str(root / "gone.sock"),
            "authkey": "11" * 32, "host": "tui", "started_at": "x",
        }))
        code, payload, _ = _cli(root, "pick", "2", "--json", capsys=capsys)
        assert code == 3 and payload == {
            "contract": CONTRACT, "op": "pick", "result": "no-engine"
        }

    def test_server_start_sweeps_dead_pid_files(self, root, servers):
        directory = root / engine_control.ENDPOINT_DIRNAME
        directory.mkdir()
        stale = directory / "999999-deadbeef.json"
        stale.write_text("{}")
        with patch(
            "claude_swap.process_detection.is_pid_alive",
            side_effect=lambda pid: pid != 999999,
        ):
            _serve(root, servers, FakeEngine())
        assert not stale.exists()

    def test_multiple_engines_exit_1_nothing_persisted(self, root, servers, capsys):
        a, b = FakeEngine(), FakeEngine()
        _serve(root, servers, a, host="auto")
        _serve(root, servers, b, host="tui")
        for argv in (["status"], ["pick", "2"], ["strategy", "consume-first"]):
            code, payload, _ = _cli(root, *argv, "--json", capsys=capsys)
            assert code == 1, argv
            assert payload["result"] == "error"
            assert payload["error"] == "multiple-engines"
        assert not (root / "settings.json").exists()
        assert a.requests == [] and b.requests == []
        assert a.applied == [] and b.applied == []

    def test_wrong_authkey_is_transport_error(self, root, servers, capsys):
        server = _serve(root, servers, FakeEngine())
        data = json.loads(server.endpoint_path.read_text())
        data["authkey"] = "22" * 32
        server.endpoint_path.write_text(json.dumps(data))
        code, payload, _ = _cli(root, "status", "--json", capsys=capsys)
        assert code == 1 and payload["error"] == "transport"

    def test_authkey_never_in_output(self, root, servers, capsys, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        engine = FakeEngine(respond=lambda t: (TickOutcome.SWITCHED, _switched("4", t), t))
        server = _serve(root, servers, engine)
        key = json.loads(server.endpoint_path.read_text())["authkey"]
        seen = []
        for argv in (["status"], ["pick", "2"], ["strategy", "best"]):
            for flag in (["--json"], []):
                engine_control.run([*argv, *flag], backup_root=root)
                out = capsys.readouterr()
                seen.append(out.out + out.err)
        blob = "".join(seen) + caplog.text
        assert key not in blob
        assert key not in repr(engine_control.discover(root))

    def test_server_start_sweeps_live_pid_with_dead_address(self, root, servers):
        """Windows can report an exited pid alive (a handle pins it): a file
        whose address answers nothing is stale either way."""
        directory = root / engine_control.ENDPOINT_DIRNAME
        directory.mkdir()
        dead = directory / f"{os.getpid()}-0badc0de.json"
        dead.write_text(json.dumps({
            "pid": os.getpid(),
            "address": r"\\.\pipe\cswap-test-gone" if sys.platform == "win32"
            else str(root / "gone.sock"),
        }))
        other = directory / "notes.json"  # not an endpoint name: never touched
        other.write_text("{}")
        live = _serve(root, servers, FakeEngine())  # a real, answering server
        _serve(root, servers, FakeEngine())  # its sweep must keep `live`
        assert not dead.exists()
        assert other.exists()
        assert live.endpoint_path.exists()

    def test_address_probe(self, root, servers):
        server = _serve(root, servers, FakeEngine())
        address = json.loads(server.endpoint_path.read_text())["address"]
        assert engine_control._address_answers(address) is True
        gone = (r"\\.\pipe\cswap-test-gone" if sys.platform == "win32"
                else str(root / "gone.sock"))
        assert engine_control._address_answers(gone) is False

    @pytest.mark.parametrize(
        "exc, code, result",
        [
            (FileNotFoundError(2, "gone"), 3, "no-engine"),
            (ConnectionRefusedError(111, "refused"), 3, "no-engine"),
            (OSError(22, "pipe busy"), 1, "error"),
            (TimeoutError(121, "semaphore timeout"), 1, "error"),
        ],
    )
    def test_connect_failure_stale_vs_unreachable(self, root, capsys, monkeypatch,
                                                  exc, code, result):
        """Only missing / refused is stale; any other OSError is a live engine
        that did not answer: transport (exit 1), never no-engine."""
        directory = root / engine_control.ENDPOINT_DIRNAME
        directory.mkdir()
        (directory / f"{os.getpid()}-0badc0de.json").write_text(json.dumps({
            "pid": os.getpid(), "address": "unused", "authkey": "11" * 32,
            "host": "auto",
        }))

        def client(address):
            raise exc

        monkeypatch.setattr(engine_control.mpc, "Client", client)
        got, payload, _ = _cli(root, "status", "--json", capsys=capsys)
        assert got == code and payload["result"] == result
        if result == "error":
            assert payload["error"] == "transport"


# ---------------------------------------------------------------------------
# handshake: bounded, mutual, constant-time
# ---------------------------------------------------------------------------


class TestHandshake:
    def _address(self, server):
        return json.loads(server.endpoint_path.read_text())["address"]

    def test_silent_peers_leave_no_handler_threads(self, root, servers, capsys,
                                                   monkeypatch):
        from multiprocessing import connection as mpc

        monkeypatch.setattr(engine_control, "_HANDSHAKE_S", 0.3)
        before = {t.ident for t in threading.enumerate()}
        server = _serve(root, servers, FakeEngine())
        silent = [mpc.Client(self._address(server)) for _ in range(5)]
        try:
            def handlers():
                return [t for t in threading.enumerate()
                        if t.name == "cswap-engine-request" and t.ident not in before]

            deadline = time.monotonic() + 5.0
            while handlers() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert handlers() == []
            # the listener kept serving throughout
            code, payload, _ = _cli(root, "status", "--json", capsys=capsys)
            assert code == 0 and payload["result"] == "running"
        finally:
            for c in silent:
                c.close()

    def test_handler_cap_sheds_excess_and_keeps_serving(self, root, servers,
                                                        capsys, monkeypatch):
        from multiprocessing import connection as mpc

        cap, burst = 2, 6
        monkeypatch.setattr(engine_control, "_HANDSHAKE_S", 0.5)
        monkeypatch.setattr(engine_control, "_MAX_HANDLERS", cap)
        before = {t.ident for t in threading.enumerate()}

        def handlers():
            return [t for t in threading.enumerate()
                    if t.name == "cswap-engine-request" and t.ident not in before]

        server = _serve(root, servers, FakeEngine())
        silent = [mpc.Client(self._address(server)) for _ in range(burst)]
        try:
            assert len(handlers()) <= cap
            shed = 0  # past the cap: closed at once, EOF instead of a hello
            for c in silent:
                try:
                    if c.poll(2.0):
                        c.recv_bytes(256)
                except (EOFError, OSError):
                    shed += 1
            assert shed >= burst - cap
            deadline = time.monotonic() + 5.0
            while handlers() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert handlers() == []
            code, payload, _ = _cli(root, "status", "--json", capsys=capsys)
            assert code == 0 and payload["result"] == "running"
        finally:
            for c in silent:
                c.close()

    def test_wrong_key_client_gets_no_proof_and_no_reply(self, root, servers):
        from multiprocessing import connection as mpc

        engine = FakeEngine()
        server = _serve(root, servers, engine)
        conn = mpc.Client(self._address(server))
        try:
            # the server closes instead of proving itself: EOF, never a proof
            with pytest.raises((engine_control._HandshakeFailed, EOFError, OSError)):
                engine_control._client_handshake(conn, b"\x22" * 32)
        finally:
            conn.close()
        ep = engine_control.Endpoint(
            pid=os.getpid(), address=self._address(server),
            authkey=b"\x33" * 32, host="auto",
        )
        with pytest.raises(engine_control._AuthFailed):
            engine_control._connect(ep)
        assert engine.requests == []

    def test_server_rejects_bad_proof_and_closes(self, root, servers):
        """A client that never knew the key: the server sends no proof back."""
        from multiprocessing import connection as mpc

        server = _serve(root, servers, FakeEngine())
        conn = mpc.Client(self._address(server))
        try:
            assert conn.poll(5)
            hello = conn.recv_bytes(256)
            assert hello.startswith(engine_control._HS_HELLO)
            conn.send_bytes(b"\x00" * 32 + b"\x01" * 32)
            assert conn.poll(5)
            with pytest.raises((EOFError, OSError)):
                conn.recv_bytes(256)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# pick
# ---------------------------------------------------------------------------


class TestPick:
    def test_switched_exit_0_contract_shape(self, root, servers, capsys):
        engine = FakeEngine(respond=lambda t: (TickOutcome.SWITCHED, _switched("4", t), t))
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "pick", "2", "--json", capsys=capsys)
        assert code == 0
        assert payload == {
            "contract": CONTRACT, "op": "pick", "result": "switched",
            "from": "4", "to": "2", "pinned": "2", "dry_run": False,
            "message": "switched #4 -> #2 (manual pick)",
        }
        assert engine.requests[0][0] == "2"

    def test_dry_run_would_switch_exit_0(self, root, servers, capsys):
        engine = FakeEngine(
            dry_run=True,
            respond=lambda t: (TickOutcome.SWITCHED, _switched("4", t, dry=True), None),
        )
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "pick", "2", "--json", capsys=capsys)
        assert code == 0
        assert payload["result"] == "would-switch"
        assert payload["dry_run"] is True and payload["pinned"] is None

    def test_api_key_target_not_pinned(self, root, servers, capsys):
        engine = FakeEngine(respond=lambda t: (
            TickOutcome.SWITCHED,
            _switched("4", t, note="API-key account: not held (no quota to measure)"),
            None,
        ))
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "pick", "5", "--json", capsys=capsys)
        assert code == 0 and payload["pinned"] is None

    def test_refused_exit_5_reason_and_no_email(self, root, servers, capsys):
        engine = FakeEngine(respond=lambda t: (
            TickOutcome.NO_ACTION,
            [NoSwitchEvent(reason="already-active",
                           detail="Account-4 (user4@example.com) is the active account")],
            None,
        ))
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "pick", "4", "--json", capsys=capsys)
        assert code == 5
        assert set(payload) == {"contract", "op", "result", "reason", "message"}
        assert payload["result"] == "refused" and payload["reason"] == "already-active"
        assert "@" not in payload["message"]

    def test_error_event_is_exit_1_engine_error(self, root, servers, capsys):
        from claude_swap.autoswitch import ErrorEvent

        engine = FakeEngine(respond=lambda t: (
            TickOutcome.ERROR, [ErrorEvent(message="could not freshen")], None,
        ))
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "pick", "2", "--json", capsys=capsys)
        assert code == 1 and payload["error"] == "engine-error"

    def test_correlates_only_the_consuming_ticks_events(self):
        """The reply is built from the events handed over for THAT tick,
        never from whatever the engine emitted before or around it."""
        engine = FakeEngine()
        before = [NoSwitchEvent(reason="below-threshold")]
        tick = _switched("4", "2")
        reply = engine_control.pick_reply(engine, TickOutcome.SWITCHED, tick)
        assert reply["result"] == "switched"
        reply = engine_control.pick_reply(engine, TickOutcome.NO_ACTION, before)
        assert reply["reason"] == "below-threshold"

    def test_pending_exit_4_then_busy_then_resolves(self, root, servers, capsys):
        engine = FakeEngine()  # never answers on its own
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "pick", "2", "--timeout", "0.3", "--json",
                                capsys=capsys)
        assert code == 4
        assert payload["result"] == "pending" and payload["op"] == "pick"
        _, status, _ = _cli(root, "status", "--json", capsys=capsys)
        assert status["engine"]["pick_pending"] is True
        code, busy, _ = _cli(root, "pick", "3", "--json", capsys=capsys)
        assert code == 5 and busy["reason"] == "busy"
        code, busy, _ = _cli(root, "strategy", "best", "--json", capsys=capsys)
        assert code == 1 and busy["error"] == "busy"
        assert not (root / "settings.json").exists()
        assert len(engine.requests) == 1  # the busy pick never reached the engine
        # the tick that consumed the first request lands late
        cb = engine.consume()
        cb(TickOutcome.SWITCHED, _switched("4", "2"))
        _, status, _ = _cli(root, "status", "--json", capsys=capsys)
        assert status["engine"]["pick_pending"] is False

    @pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_timeout_is_the_default(self, root, servers, monkeypatch,
                                               timeout):
        monkeypatch.setattr(engine_control, "DEFAULT_TIMEOUT", 0.3)
        server = _serve(root, servers, FakeEngine())  # never answers
        start = time.monotonic()
        reply = server.handle({"op": "pick", "target": "2", "timeout": timeout})
        assert reply["result"] == "pending"
        assert 0.2 <= time.monotonic() - start < 5.0

    def test_concurrent_second_pick_is_busy(self, root, servers, capsys):
        engine = FakeEngine()
        _serve(root, servers, engine)
        first: dict = {}

        def slow():
            first["code"] = engine_control.run(
                ["pick", "2", "--timeout", "5", "--json"], backup_root=root
            )

        t = threading.Thread(target=slow)
        t.start()
        for _ in range(100):
            if engine.requests:
                break
            time.sleep(0.02)
        code = engine_control.run(["pick", "3", "--json"], backup_root=root)
        assert code == 5
        cb = engine.consume()
        cb(TickOutcome.SWITCHED, _switched("4", "2"))
        t.join(10)
        assert first["code"] == 0
        out = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln]
        assert {"busy"} <= {p.get("reason") for p in out}

    def test_superseded_by_tui_m(self, root, servers, capsys):
        engine = FakeEngine()
        _serve(root, servers, engine)
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(code=engine_control.run(
            ["pick", "2", "--timeout", "5", "--json"], backup_root=root)))
        t.start()
        for _ in range(100):
            if engine.requests:
                break
            time.sleep(0.02)
        engine.request_switch(target="3")  # the TUI's own `m`, no callback
        t.join(10)
        assert result["code"] == 5
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["reason"] == "superseded"

    def test_engine_rebuilt_before_consuming_is_engine_stopped(
        self, root, servers, capsys
    ):
        """TUI `s`/`l` swap the engine under a pending pick: the old engine
        never ticks again, so the request is withdrawn and answered."""
        holder = {"engine": FakeEngine()}
        server = engine_control.start_server(
            lambda: holder["engine"], host="tui", backup_root=root
        )
        servers.append(server)
        old = holder["engine"]
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(code=engine_control.run(
            ["pick", "2", "--timeout", "5", "--json"], backup_root=root)))
        t.start()
        for _ in range(100):
            if old.requests:
                break
            time.sleep(0.02)
        holder["engine"] = FakeEngine()
        t.join(10)
        assert result["code"] == 1
        assert json.loads(capsys.readouterr().out)["error"] == "engine-stopped"

    def test_server_stop_answers_unconsumed_pick(self, root, servers, capsys):
        engine = FakeEngine()
        server = _serve(root, servers, engine)
        result: dict = {}
        t = threading.Thread(target=lambda: result.update(code=engine_control.run(
            ["pick", "2", "--timeout", "5", "--json"], backup_root=root)))
        t.start()
        for _ in range(100):
            if engine.requests:
                break
            time.sleep(0.02)
        engine_control.stop_server(server)
        t.join(10)
        assert result["code"] == 1
        assert json.loads(capsys.readouterr().out)["error"] == "engine-stopped"

    def test_reply_failure_is_engine_error_and_not_stuck_busy(
        self, root, servers, capsys
    ):
        engine = FakeEngine(respond=lambda t: (TickOutcome.SWITCHED, _switched("4", t), t))
        _serve(root, servers, engine)
        with patch.object(engine_control, "pick_reply",
                          side_effect=RuntimeError("boom")):
            code, payload, _ = _cli(root, "pick", "2", "--json", capsys=capsys)
        assert code == 1 and payload["error"] == "engine-error"
        _, status, _ = _cli(root, "status", "--json", capsys=capsys)
        assert status["engine"]["pick_pending"] is False
        code, payload, _ = _cli(root, "pick", "3", "--json", capsys=capsys)
        assert code == 0 and payload["result"] == "switched"

    def test_pinned_read_from_current_engine(self, root, servers, capsys):
        """The tick ran on an engine the TUI then retired: report the pin of
        the engine that is running now."""
        holder: dict = {}

        def respond(t):
            holder["engine"] = FakeEngine(pinned="7")  # rebuilt mid-tick
            return TickOutcome.SWITCHED, _switched("4", t), None

        holder["engine"] = FakeEngine(respond=respond)
        _serve(root, servers, lambda: holder["engine"])
        code, payload, _ = _cli(root, "pick", "2", "--json", capsys=capsys)
        assert code == 0 and payload["pinned"] == "7"

    def test_outcome_landing_at_the_deadline_is_returned(self, root, servers):
        class NeverWaits(threading.Event):
            def wait(self, timeout=None):
                return False

        class LateEngine(FakeEngine):
            def request_switch(self, target=None, *, on_result=None):
                on_result.done = NeverWaits()
                on_result(TickOutcome.SWITCHED, _switched("4", target))

        server = _serve(root, servers, LateEngine())
        reply = server.handle({"op": "pick", "target": "2", "timeout": 0.2})
        assert reply["result"] == "switched"

    def test_no_engine_exit_3_writes_nothing(self, root, capsys):
        before = _tree(root)
        code, payload, _ = _cli(root, "pick", "2", "--json", capsys=capsys)
        assert code == 3 and payload["result"] == "no-engine"
        assert _tree(root) == before

    @pytest.mark.parametrize("argv", [
        ["pick", "two"], ["pick", "0"], ["pick", "-1"], ["pick"],
        ["pick", "2", "--timeout", "0"], ["pick", "2", "--timeout", "x"],
        ["strategy", "fastest"], ["strategy"], [], ["bogus"],
    ])
    def test_usage_errors_exit_2(self, root, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            engine_control.run(argv, backup_root=root)
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# pick through the REAL engine tick
# ---------------------------------------------------------------------------


class TestRealEngine:
    _USAGE = {"1": _usage7(30, 30), "2": _usage7(25, 25), "3": _usage7(60, 60)}

    def _pick_while_ticking(self, h, root, servers, target, usage, *, dry=False):
        if dry:
            h.engine = h._make_engine(dry_run=True)
        engine = h.engine
        _serve(root, servers, lambda: engine)
        result: dict = {}

        def client():
            result["code"] = engine_control.run(
                ["pick", target, "--timeout", "10", "--json"], backup_root=root
            )

        t = threading.Thread(target=client)
        t.start()
        for _ in range(250):
            if engine._switch_now.is_set():
                break
            time.sleep(0.02)
        assert engine._switch_now.is_set()
        # An unrelated tick's events must not leak into the answer: only the
        # tick below consumes the request.
        h.events.append(NoSwitchEvent(reason="cooldown"))
        h.tick_with_usage(usage)
        t.join(15)
        return result["code"]

    def test_live_pick_switches_and_pins(self, harness, root, servers, capsys):
        code = self._pick_while_ticking(harness, root, servers, "3", self._USAGE)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["result"] == "switched"
        assert (payload["from"], payload["to"], payload["pinned"]) == ("1", "3", "3")
        assert payload["dry_run"] is False
        assert harness.active_number() == 3
        assert "@" not in json.dumps(payload)

    def test_dry_run_engine_would_switch(self, harness, root, servers, capsys):
        code = self._pick_while_ticking(harness, root, servers, "3", self._USAGE,
                                        dry=True)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["result"] == "would-switch"
        assert payload["dry_run"] is True and payload["pinned"] is None
        assert harness.active_number() == 1

    def test_refusal_reason_from_the_tick(self, harness, root, servers, capsys):
        code = self._pick_while_ticking(harness, root, servers, "1", self._USAGE)
        payload = json.loads(capsys.readouterr().out)
        assert code == 5 and payload["reason"] == "already-active"


class TestEngineHook:
    """The engine-side hook: minimal, and inert without a callback."""

    def test_callback_gets_the_consuming_ticks_outcome(self, harness):
        got = []
        harness.tick_with_usage(TestRealEngine._USAGE)  # no request: no call
        harness.engine.request_switch(target="3", on_result=lambda o, e: got.append((o, e)))
        assert got == []
        harness.tick_with_usage(TestRealEngine._USAGE)
        assert len(got) == 1
        outcome, events = got[0]
        assert outcome is TickOutcome.SWITCHED
        assert [e.kind for e in events if e.kind == "switch"] == ["switch"]
        harness.tick_with_usage(TestRealEngine._USAGE)  # one-shot
        assert len(got) == 1
        assert harness.engine.last_tick_at is not None

    def test_replaced_request_is_told_superseded(self, harness):
        got = []
        harness.engine.request_switch(target="3", on_result=lambda o, e: got.append(o))
        harness.engine.request_switch(target="2")  # the TUI's `m`
        assert got == [None]
        harness.tick_with_usage(TestRealEngine._USAGE)
        assert got == [None]
        assert harness.active_number() == 2

    def test_withdraw_only_unconsumed_and_matching(self, harness):
        def cb(o, e):
            pass

        harness.engine.request_switch(target="3", on_result=cb)
        assert harness.engine.withdraw_switch(lambda o, e: None) is False
        assert harness.engine.withdraw_switch(cb) is True
        harness.tick_with_usage(TestRealEngine._USAGE)
        assert harness.active_number() == 1  # withdrawn: never ran

    def test_m_without_callback_unchanged(self, harness):
        harness.engine.request_switch(target="3")
        assert harness.tick_with_usage(TestRealEngine._USAGE) is TickOutcome.SWITCHED
        assert harness.engine.pinned == "3"
        assert harness.engine._tick_events is None

    def test_apply_strategy_keeps_pin_and_wakes(self, harness):
        harness.engine.request_switch(target="3")
        harness.tick_with_usage(TestRealEngine._USAGE)
        harness.engine._wake.clear()
        harness.engine.apply_strategy("consume-first")
        assert harness.engine.settings.strategy == "consume-first"
        assert harness.engine.pinned == "3"
        assert harness.engine._wake.is_set()


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------


class TestStrategy:
    def test_auto_host_persists_and_applies_live(self, root, servers, capsys):
        engine = FakeEngine(strategy="best", pinned="2")
        _serve(root, servers, engine)
        code, payload, _ = _cli(root, "strategy", "consume-first-reserve-jj", "--json",
                                capsys=capsys)
        assert code == 0
        assert payload == {
            "contract": CONTRACT, "op": "strategy", "result": "applied",
            "strategy": "consume-first-reserve-jj", "persisted": True, "live": True,
        }
        on_disk = json.loads((root / "settings.json").read_text())
        assert on_disk["autoswitch"]["strategy"] == "consume-first-reserve-jj"
        assert engine.applied == ["consume-first-reserve-jj"]
        assert engine.pinned == "2"
        _, status, _ = _cli(root, "status", "--json", capsys=capsys)
        assert status["engine"]["strategy"] == "consume-first-reserve-jj"
        assert status["engine"]["strategy_source"] == "settings"
        assert status["engine"]["reserve"] == ["1"]

    def test_auto_host_default_apply_uses_real_engine(self, harness, root, servers,
                                                      capsys):
        engine = harness.engine
        _serve(root, servers, lambda: engine)
        code, _, _ = _cli(root, "strategy", "consume-first", "--json", capsys=capsys)
        assert code == 0
        assert engine.settings.strategy == "consume-first"

    def test_no_engine_exit_3_persists_nothing(self, root, capsys):
        before = _tree(root)
        code, payload, _ = _cli(root, "strategy", "best", "--json", capsys=capsys)
        assert code == 3 and payload["result"] == "no-engine"
        assert _tree(root) == before

    def test_live_apply_failure_reports_persisted_not_live(self, root, servers,
                                                           capsys):
        def boom(name):
            raise RuntimeError("nope")

        _serve(root, servers, FakeEngine(), apply_strategy=boom)
        code, payload, _ = _cli(root, "strategy", "best", "--json", capsys=capsys)
        assert code == 1
        assert payload["persisted"] is True and payload["live"] is False


# ---------------------------------------------------------------------------
# hosts
# ---------------------------------------------------------------------------


class TestAutoHost:
    class LoopEngine:
        instances: list = []

        def __init__(self, switcher, settings, on_event, *, dry_run=False, **kw):
            self.switcher = switcher
            self.settings = settings
            self.dry_run = dry_run
            self.pinned = None
            self.seen_endpoint = None
            type(self).instances.append(self)

        def tick(self):
            return TickOutcome.NO_ACTION

        def run_loop(self):
            from claude_swap.paths import get_backup_root

            d = get_backup_root() / engine_control.ENDPOINT_DIRNAME
            self.seen_endpoint = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
            return 0

        def stop(self):
            pass

    def _run(self, argv):
        with patch("claude_swap.autoswitch.AutoSwitchEngine", self.LoopEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "auto", *argv]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        return exc.value.code

    def test_once_opens_no_channel(self, temp_home):
        self.LoopEngine.instances = []
        with patch.object(engine_control, "start_server") as start:
            assert self._run(["--once"]) == 2
        start.assert_not_called()

    def test_loop_opens_and_removes_channel(self, temp_home):
        from claude_swap.paths import get_backup_root

        self.LoopEngine.instances = []
        assert self._run([]) == 0
        engine = self.LoopEngine.instances[-1]
        assert len(engine.seen_endpoint) == 1  # open while the loop ran
        d = get_backup_root() / engine_control.ENDPOINT_DIRNAME
        assert list(d.iterdir()) == []  # removed on exit

    def test_loop_says_when_channel_unavailable(self, temp_home, capsys):
        self.LoopEngine.instances = []
        with patch.object(engine_control, "start_server", return_value=None):
            assert self._run([]) == 0
        assert "engine control unavailable" in capsys.readouterr().out

    def test_json_loop_says_unavailable_on_stderr_only(self, temp_home, capsys):
        """--json stdout is a JSONL event stream: the notice must not pollute it."""
        self.LoopEngine.instances = []
        with patch.object(engine_control, "start_server", return_value=None):
            assert self._run(["--json"]) == 0
        out = capsys.readouterr()
        assert "engine control unavailable" in out.err
        assert "engine control unavailable" not in out.out

    def test_cli_dispatches_engine_verb(self, temp_home, capsys):
        with patch.object(sys, "argv", ["claude-swap", "engine", "status", "--json"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 3
        assert json.loads(capsys.readouterr().out)["result"] == "no-engine"


@pytest.mark.asyncio
class TestTuiHost:
    async def test_strategy_rebuilds_like_s_and_persists(self, tmp_path, monkeypatch):
        import asyncio

        from tests.test_tui import (
            FakeSwitcher, _FakeEngine, make_account, make_app, settle,
        )

        _FakeEngine.instances = []
        monkeypatch.setattr("claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine)
        (tmp_path / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"strategy": "consume-first", "reserveAccounts": "none"},
        }))
        app = make_app(FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        ))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("g")
            await pilot.pause()
            await settle(pilot)
            screen = app.screen
            assert screen._control is not None
            _FakeEngine.instances[0].pinned = "2"
            code = await asyncio.to_thread(
                engine_control.run, ["strategy", "best", "--json"], backup_root=tmp_path
            )
            await settle(pilot)
            assert code == 0
            assert len(_FakeEngine.instances) == 2
            assert _FakeEngine.instances[0].stopped is True
            new = _FakeEngine.instances[1]
            assert new.settings.strategy == "best"
            assert new.dry_run is True
            assert new.kwargs.get("pinned") == "2"  # held pick carried over
            on_disk = json.loads((tmp_path / "settings.json").read_text())
            assert on_disk["autoswitch"]["strategy"] == "best"
            assert on_disk["autoswitch"]["reserveAccounts"] == "none"
            from textual.widgets import Static

            summary = screen.query_one("#auto-summary", Static).render().plain
            assert "best" in summary and "(session)" not in summary
            code = await asyncio.to_thread(
                engine_control.run, ["status", "--json"], backup_root=tmp_path
            )
            assert code == 0
            await pilot.press("escape")
            await settle(pilot)
            assert list((tmp_path / engine_control.ENDPOINT_DIRNAME).iterdir()) == []

    async def test_strategy_after_escape_starts_no_orphan_engine(
        self, tmp_path, monkeypatch
    ):
        """A queued `call_from_thread` landing after Escape's unmount must not
        rebuild a LIVE engine with no UI: persisted, not live."""
        import asyncio

        from tests.test_tui import (
            FakeSwitcher, _FakeEngine, make_account, make_app, settle,
        )

        _FakeEngine.instances = []
        monkeypatch.setattr("claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine)
        app = make_app(FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        ))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("g")
            await pilot.pause()
            await settle(pilot)
            screen = app.screen
            apply = screen._control._apply_strategy  # the hook the server holds
            await pilot.press("escape")
            await settle(pilot)
            assert screen._control is None
            # the request that raced the unmount (server half, no transport)
            late = engine_control.ControlServer(
                lambda: FakeEngine(), host="tui", backup_root=tmp_path,
                apply_strategy=apply,
            )
            reply = await asyncio.to_thread(
                late.handle, {"op": "strategy", "strategy": "best"}
            )
            await settle(pilot)
            assert reply["result"] == "error" and reply["error"] == "engine-error"
            assert reply["persisted"] is True and reply["live"] is False
            assert len(_FakeEngine.instances) == 1  # no orphan rebuild
            assert _FakeEngine.instances[0].stopped is True
            on_disk = json.loads((tmp_path / "settings.json").read_text())
            assert on_disk["autoswitch"]["strategy"] == "best"

    async def test_control_unavailable_is_logged(self, tmp_path, monkeypatch):
        from textual.widgets import RichLog

        from tests.test_tui import (
            FakeSwitcher, _FakeEngine, make_account, make_app, settle,
        )

        _FakeEngine.instances = []
        monkeypatch.setattr("claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine)
        monkeypatch.setattr(engine_control, "start_server", lambda *a, **k: None)
        app = make_app(FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        ))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("g")
            await pilot.pause()
            await settle(pilot)
            log = app.screen.query_one("#event-log", RichLog)
            text = "\n".join(strip.text for strip in log.lines)
            assert "engine control unavailable" in text

    async def test_m_key_unchanged(self, tmp_path, monkeypatch):
        from tests.test_tui import (
            FakeSwitcher, _FakeEngine, make_account, make_app, settle,
        )

        _FakeEngine.instances = []
        monkeypatch.setattr("claude_swap.tui.autoview.AutoSwitchEngine", _FakeEngine)
        app = make_app(FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        ))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("g")
            await pilot.pause()
            await settle(pilot)
            app.screen._on_manual_pick("2", [])
            assert _FakeEngine.instances[0].switch_targets == ["2"]


# ---------------------------------------------------------------------------
# wake (fork: fleet) -- `fleet receive` nudges the local engine
# ---------------------------------------------------------------------------


class _WakeableEngine(FakeEngine):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.woken = threading.Event()

    def wake(self):
        self.woken.set()


class TestWake:
    def test_wake_engine_reaches_a_running_engine(self, root, servers):
        engine = _WakeableEngine()
        _serve(root, servers, engine)
        assert engine_control.wake_engine(root) is True
        assert engine.woken.is_set()

    def test_falls_back_to_the_wake_event(self, root, servers):
        engine = FakeEngine()
        engine._wake = threading.Event()
        _serve(root, servers, engine)
        assert engine_control.wake_engine(root) is True
        assert engine._wake.is_set()

    def test_no_engine_is_false(self, root):
        assert engine_control.wake_engine(root) is False

    def test_engine_without_wake_is_false(self, root, servers):
        _serve(root, servers, FakeEngine())  # no wake(), no _wake: engine-error
        assert engine_control.wake_engine(root) is False

    def test_never_raises(self, root, monkeypatch):
        def boom(*a, **k):
            raise OSError("pipe")

        monkeypatch.setattr(engine_control, "request", boom)
        assert engine_control.wake_engine(root) is False

    def test_default_timeout_is_short(self, root, monkeypatch):
        seen = {}

        def fake_request(backup_root, message, *, reply_timeout):
            seen.update(message=message, timeout=reply_timeout)
            return {"result": "woken"}

        monkeypatch.setattr(engine_control, "request", fake_request)
        assert engine_control.wake_engine(root) is True
        assert seen == {"message": {"op": "wake"}, "timeout": engine_control.WAKE_TIMEOUT_S}
        assert engine_control.WAKE_TIMEOUT_S <= 2.0
