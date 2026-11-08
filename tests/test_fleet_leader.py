"""Fork (fleet): FleetLeader, its engine lifecycle, and the status block.

No real ssh: every test injects a fake ``runner`` into the real
``fleet_remote.run_remote``, and an autouse guard fails any call that would
reach ``subprocess.run``. ``build_lease`` and ``load_hosts`` are stubbed by
their Internal API names.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from claude_swap import engine_control, fleet_lease, fleet_leader, fleet_remote
from claude_swap.autoswitch import TickOutcome
from claude_swap.fleet_leader import FleetLeader
from claude_swap.fleet_remote import Host

from tests.test_autoswitch import EngineHarness, _usage


@pytest.fixture(autouse=True)
def no_real_ssh(monkeypatch):
    real = fleet_remote.run_remote

    def guarded(host, args, stdin_obj, *, timeout=20.0, runner=subprocess.run):
        if runner is subprocess.run:
            pytest.fail("a test reached real ssh")
        return real(host, args, stdin_obj, timeout=timeout, runner=runner)

    monkeypatch.setattr(fleet_remote, "run_remote", guarded)


class FakeSSH:
    """subprocess.run stand-in: replies per ssh alias; ``hang`` blocks."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.fail: set[str] = set()
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        alias = argv[5]
        with self.lock:
            self.calls.append((alias, json.loads(kwargs["input"])))
            self.argvs.append(list(argv))
        if alias == "hang":
            self.release.wait(10)
        if alias in self.fail:
            return SimpleNamespace(returncode=255, stdout="", stderr="no route")
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"host": alias, "ok": True}), stderr=""
        )


def _hosts(*specs):
    out = []
    for name, override in specs:
        out.append(Host(name=name, alias=name, override=override or {"mode": "follow"}))
    return out


@pytest.fixture
def stubs(monkeypatch):
    state = SimpleNamespace(hosts=[], leases=[])

    monkeypatch.setattr(fleet_remote, "load_hosts", lambda root, **kw: list(state.hosts))

    def build_lease(switcher, *, leader_id, seq, target, settings, ttl_s=180):
        lease = {"leader": leader_id, "seq": seq, "target": target,
                 "settings": settings, "ttl_s": ttl_s}
        state.leases.append(lease)
        return lease

    monkeypatch.setattr(fleet_lease, "build_lease", build_lease)
    return state


@pytest.fixture
def h(temp_home):
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _hosts_file(h):
    d = h.switcher.backup_dir / "fleet"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hosts.json").write_text("{}", encoding="utf-8")


def _leader(h, runner, **kw):
    return FleetLeader(h.switcher, h.engine, h.switcher.backup_dir,
                       runner=runner, interval_s=3600, **kw)


class TestLeaderRound:
    def test_targets_follow_pin_strategy_and_live_settings(self, h, stubs):
        stubs.hosts = _hosts(
            ("w1", None),
            ("w2", {"mode": "pin", "account": "3"}),
            ("w3", {"mode": "strategy", "strategy": "consume-first"}),
        )
        h.engine.apply_threshold(80.0)  # a session value, never persisted
        leader = _leader(h, FakeSSH())
        assert leader.run_round() == 3
        targets = [lease["target"] for lease in stubs.leases]
        assert targets == [
            {"mode": "follow", "account": "1", "strategy": None},
            {"mode": "pin", "account": "3", "strategy": None},
            {"mode": "strategy", "account": None, "strategy": "consume-first"},
        ]
        settings = stubs.leases[0]["settings"]
        assert settings["threshold"] == 80.0
        assert settings["strategy"] == "best"
        assert set(settings["accounts"]) == {"1", "2", "3"}
        assert {lease["leader"] for lease in stubs.leases} == {leader.leader_id}

    def test_snapshot_keeps_last_result_per_host(self, h, stubs):
        stubs.hosts = _hosts(("good", None), ("down", None))
        ssh = FakeSSH()
        ssh.fail.add("down")
        leader = _leader(h, ssh)
        leader.start()
        try:
            leader.kick()
            assert _wait(lambda: len(leader.snapshot()["hosts"]) == 2)
        finally:
            leader.stop()
        snap = leader.snapshot()
        assert snap["leader_id"] == leader.leader_id
        good, down = snap["hosts"]["good"], snap["hosts"]["down"]
        assert good["reachable"] is True and good["error"] is None
        assert good["snapshot"] == {"host": "good", "ok": True}
        assert good["last_ok_at"].endswith("Z")
        assert down["reachable"] is False and "255" in down["error"]
        assert down["snapshot"] is None and down["last_ok_at"] is None
        # The lease went on stdin, never on the command line.
        assert all(a[-3:] == ["fleet", "receive", "--json"] for a in ssh.argvs)
        assert all(p["target"]["mode"] == "follow" for _, p in ssh.calls)

    def test_a_hung_host_never_delays_the_others(self, h, stubs):
        stubs.hosts = _hosts(("hang", None), ("fast", None))
        ssh = FakeSSH()
        leader = _leader(h, ssh)
        leader.start()
        try:
            assert _wait(lambda: "fast" in leader.snapshot()["hosts"])
            assert "hang" not in leader.snapshot()["hosts"]
            # Next round: the hung host is still in flight, so it is skipped
            # (never piles up), and the healthy one is served again.
            before = sum(1 for a, _ in ssh.calls if a == "fast")
            assert leader.run_round() == 1
            assert _wait(lambda: sum(1 for a, _ in ssh.calls if a == "fast") > before)
            ssh.release.set()
            assert _wait(lambda: "hang" in leader.snapshot()["hosts"])
        finally:
            ssh.release.set()
            leader.stop()

    def test_pool_is_bounded(self, h, stubs):
        leader = FleetLeader(h.switcher, h.engine, h.switcher.backup_dir,
                             runner=FakeSSH(), max_workers=50)
        leader.start()
        try:
            assert len(leader._workers) == fleet_leader.MAX_WORKERS
        finally:
            leader.stop()

    def test_bad_lease_build_is_recorded_not_raised(self, h, stubs, monkeypatch):
        stubs.hosts = _hosts(("w1", None))
        monkeypatch.setattr(fleet_lease, "build_lease",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        leader = _leader(h, FakeSSH())
        assert leader.run_round() == 0
        assert leader.snapshot()["hosts"]["w1"]["reachable"] is False


class TestEngineLifecycle:
    def test_leads_only_when_live_with_hosts(self, h, stubs, monkeypatch):
        started = []
        monkeypatch.setattr(FleetLeader, "start", lambda self: started.append(self))
        fleet = h.engine._fleet
        assert h.engine.fleet_leader is None
        fleet.ensure_leader()  # no hosts file
        assert fleet.leader is None
        _hosts_file(h)
        stubs.hosts = []
        fleet.ensure_leader()  # file, but no hosts
        assert fleet.leader is None
        stubs.hosts = _hosts(("w1", None))
        fleet.ensure_leader()
        assert fleet.leader is not None and len(started) == 1
        assert h.engine.fleet_leader is fleet.leader  # the Hosts screen's handle
        fleet.ensure_leader()  # idempotent
        assert len(started) == 1

    def test_dry_run_never_leads(self, h, stubs, monkeypatch):
        monkeypatch.setattr(FleetLeader, "start", lambda self: pytest.fail("led"))
        _hosts_file(h)
        stubs.hosts = _hosts(("w1", None))
        h.engine = h._make_engine(dry_run=True)
        h.engine._fleet.in_loop = True
        h.engine._fleet.ensure_leader()
        h.tick_with_usage({"1": _usage(30), "2": _usage(20)})
        assert h.engine._fleet.leader is None

    def test_run_loop_starts_and_stops_the_leader(self, h, stubs, monkeypatch):
        _hosts_file(h)
        stubs.hosts = _hosts(("w1", None))
        seen = []

        def one_tick():
            seen.append(h.engine._fleet.leader)
            h.engine.stop()
            return TickOutcome.NO_ACTION

        monkeypatch.setattr(h.engine, "tick", one_tick)
        monkeypatch.setattr(FleetLeader, "__init__", _no_ssh_init(FleetLeader.__init__))
        assert h.engine.run_loop() == 0
        assert seen and seen[0] is not None
        assert seen[0].running is False  # stop() stopped it
        assert h.engine._fleet.leader is None
        assert h.engine.fleet_leader is None

    def test_kick_after_every_live_switch(self, h):
        kicks = []
        h.engine._fleet.leader = SimpleNamespace(kick=lambda: kicks.append(1),
                                                 stop=lambda: None, running=True)
        assert h.tick_with_usage({"1": _usage(30), "2": _usage(20)}) is TickOutcome.NO_ACTION
        assert kicks == []
        assert h.tick_with_usage({"1": _usage(95), "2": _usage(10)}) is TickOutcome.SWITCHED
        assert kicks == [1]


def _no_ssh_init(real_init):
    def init(self, *a, **kw):
        kw["runner"] = FakeSSH()
        real_init(self, *a, **kw)
    return init


class TestStatusBlock:
    def _status(self, h):
        server = engine_control.ControlServer(
            lambda: h.engine, host="auto", backup_root=h.switcher.backup_dir
        )
        return server.handle({"op": "status"})

    def test_none(self, h):
        reply = self._status(h)
        assert reply["contract"] == "cswap-engine/0.1"
        assert reply["fleet"] == {"role": "none", "leader": None, "follow": None,
                                  "refusal": None}
        assert "fleet" not in reply["engine"]  # the engine key set is unchanged

    def test_worker(self, h, monkeypatch):
        d = h.switcher.backup_dir / "fleet"
        d.mkdir()
        (d / "lease.json").write_text("{}", encoding="utf-8")
        seen = []

        def follow_state(root, engine_status, *, now=None):
            seen.append(engine_status)
            return {"state": "following", "reason": None, "lease_age_s": 3.0}

        monkeypatch.setattr(fleet_lease, "follow_state", follow_state)
        reply = self._status(h)
        assert reply["fleet"]["role"] == "worker"
        assert reply["fleet"]["follow"]["state"] == "following"
        assert seen[0]["engine"]["mode"] == "live"

    def test_leader(self, h, stubs):
        leader = _leader(h, FakeSSH())
        leader.start()
        h.engine._fleet.leader = leader
        try:
            reply = self._status(h)
        finally:
            h.engine.stop()
        fleet = reply["fleet"]
        assert fleet["role"] == "leader" and fleet["follow"] is None
        assert fleet["leader"] == {"leader_id": leader.leader_id, "hosts": {}}
        assert leader.running is False  # engine.stop() stopped it

    def test_fake_engine_without_fleet_state(self, tmp_path):
        from tests.test_engine_control import FakeEngine

        server = engine_control.ControlServer(
            lambda: FakeEngine(), host="auto", backup_root=tmp_path
        )
        assert server.handle({"op": "status"})["fleet"]["role"] == "none"
