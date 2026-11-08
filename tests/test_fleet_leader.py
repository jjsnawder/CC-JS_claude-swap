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
    state = SimpleNamespace(hosts=[], leases=[], lists=0)

    monkeypatch.setattr(fleet_remote, "load_hosts", lambda root, **kw: list(state.hosts))

    def list_payload(switcher):
        state.lists += 1
        return {"schemaVersion": 1, "accounts": [], "round": state.lists}

    def build_lease(switcher, *, leader_id, seq, target, settings, ttl_s=180, usage=None):
        lease = {"leader": leader_id, "seq": seq, "target": target,
                 "settings": settings, "ttl_s": ttl_s, "usage": usage}
        state.leases.append(lease)
        return lease

    monkeypatch.setattr(fleet_lease, "list_payload", list_payload)
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

    def test_one_store_only_list_per_round(self, h, stubs):
        stubs.hosts = _hosts(("w1", None), ("w2", None), ("w3", None))
        leader = _leader(h, FakeSSH())
        assert leader.run_round() == 3
        assert stubs.lists == 1
        assert {id(lease["usage"]) for lease in stubs.leases} == {id(stubs.leases[0]["usage"])}
        assert stubs.leases[0]["usage"]["round"] == 1

    def test_real_round_never_fetches_usage(self, h, monkeypatch):
        """The leader thread lists store-only: no oauth usage fetch."""
        from claude_swap import oauth

        monkeypatch.setattr(fleet_remote, "load_hosts",
                            lambda root, **kw: _hosts(("w1", None), ("w2", None)))
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account",
                            lambda *a, **k: pytest.fail("leader fetched usage"))
        calls = []
        real = h.switcher.list_accounts

        def spy(*a, **kw):
            calls.append(kw)
            return real(*a, **kw)

        monkeypatch.setattr(h.switcher, "list_accounts", spy)
        leader = _leader(h, FakeSSH())
        assert leader.run_round() == 2
        assert calls == [{"json_output": True, "fetch": set()}]

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

    def test_leader_start_failure_never_costs_the_tick(self, h, stubs, monkeypatch):
        """A raising leader start must not skip `_finish_switch_request`."""
        _hosts_file(h)
        stubs.hosts = _hosts(("w1", None))

        def boom(self):
            raise RuntimeError("no threads")

        monkeypatch.setattr(FleetLeader, "start", boom)
        h.engine._fleet.in_loop = True
        h.engine._fleet.ensure_leader()  # the run_loop call: no raise either
        results = []
        h.engine.request_switch(target="2", on_result=lambda o, e: results.append(o))
        assert h.tick_with_usage({"1": _usage(30), "2": _usage(20)}) is TickOutcome.SWITCHED
        assert results == [TickOutcome.SWITCHED]
        assert h.engine._fleet.leader is None
        assert h.engine.last_tick_at is not None

    def test_raising_end_tick_never_costs_the_callback(self, h, monkeypatch):
        monkeypatch.setattr(h.engine._fleet, "end_tick",
                            lambda outcome: (_ for _ in ()).throw(RuntimeError("x")))
        results = []
        h.engine.request_switch(target="2", on_result=lambda o, e: results.append(o))
        assert h.tick_with_usage({"1": _usage(30), "2": _usage(20)}) is TickOutcome.SWITCHED
        assert results == [TickOutcome.SWITCHED]

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

        def follow_state(root, engine_status, *, now=None, active=None):
            seen.append((engine_status, active))
            return {"state": "following", "reason": None, "lease_age_s": 3.0,
                    "on_target": None}

        monkeypatch.setattr(fleet_lease, "follow_state", follow_state)
        reply = self._status(h)
        assert reply["fleet"]["role"] == "worker"
        assert reply["fleet"]["follow"]["state"] == "following"
        assert seen[0][0]["engine"]["mode"] == "live"
        assert seen[0][1] == "1"  # the box's active account, for on_target

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

    def test_leader_status_carries_no_email(self, h, stubs):
        leader = _leader(h, FakeSSH())
        leader.start()
        h.engine._fleet.leader = leader
        worker_reply = {
            "contract": "cswap-fleet/1",
            "host_time": 1.0,
            "accounts": {
                "schemaVersion": 1, "activeAccountNumber": 2,
                "accounts": [
                    {"number": 1, "email": "alice@example.com", "alias": "bob@example.com",
                     "usageStatus": "ok", "usageError": "token for carol@example.com"},
                    {"number": 2, "email": "dave@example.com", "active": True},
                ],
            },
            "engine": {"result": "running", "message": "erin@example.com said hi"},
            "follow": {"state": "following", "reason": None, "lease_age_s": 1.0,
                       "on_target": True},
            "relogin": ["1"],
            "settings_hash": "abc",
            "lease": {"accepted": True, "seq": 1},
        }
        leader._record("w1", worker_reply, None)
        leader._record("w2", None, "ssh to frank@example.com failed")
        try:
            reply = self._status(h)
        finally:
            h.engine.stop()
        text = json.dumps(reply)
        assert "@" not in text
        host = reply["fleet"]["leader"]["hosts"]["w1"]
        snap = host["snapshot"]
        assert host["reachable"] is True
        assert [r["number"] for r in snap["accounts"]["accounts"]] == ["1", "2"]
        assert snap["accounts"]["activeAccountNumber"] == 2
        assert snap["follow"]["on_target"] is True and snap["settings_hash"] == "abc"
        # The raw snapshot (in-process, for the Hosts screen) is untouched.
        assert leader.snapshot()["hosts"]["w1"]["snapshot"] is worker_reply
        # Same shape as a raw reply, so fleet_status reads it unchanged.
        assert set(snap) == set(worker_reply)

    def test_strategy_reports_the_lease_or_fallback(self, h, monkeypatch):
        from tests.test_fleet_follow import LeaseBox, _lease

        box = LeaseBox(monkeypatch, h.switcher.backup_dir)
        box.mark_history()
        box.lease = _lease(mode="strategy", strategy="consume-first")
        h.tick_with_usage({"1": _usage(30), "2": _usage(20), "3": _usage(20)})
        e = self._status(h)["engine"]
        assert (e["strategy"], e["strategy_source"]) == ("consume-first", "fleet-lease")
        box.lease = None
        h.tick_with_usage({"1": _usage(30), "2": _usage(20), "3": _usage(20)})
        e = self._status(h)["engine"]
        assert (e["strategy"], e["strategy_source"]) == (
            "consume-first-reserve-jj", "fleet-fallback")
        assert h.engine.settings.strategy == "best"  # the engine's own is untouched

    def test_reserve_and_threshold_report_the_tick_in_use(self, h, monkeypatch):
        from tests.test_fleet_follow import LeaseBox, _lease

        box = LeaseBox(monkeypatch, h.switcher.backup_dir)
        box.mark_history()
        box.lease = _lease(mode="strategy", strategy="consume-first-reserve-jj",
                           settings={"reserveAccounts": "2,3", "threshold": 70.0})
        h.tick_with_usage({"1": _usage(30), "2": _usage(20), "3": _usage(20)})
        e = self._status(h)["engine"]
        assert (e["strategy"], e["strategy_source"], e["reserve"], e["threshold"]) == (
            "consume-first-reserve-jj", "fleet-lease", ["2", "3"], 70.0)
        own = h.engine.settings  # the box's own `best` has no reserve set in force
        assert (own.strategy, own.reserve_accounts, own.threshold) == ("best", "1", 90.0)

    def test_no_fleet_strategy_source_unchanged(self, h):
        h.tick_with_usage({"1": _usage(30), "2": _usage(20)})
        assert self._status(h)["engine"]["strategy_source"] in ("settings", "session")

    def test_wake_op_wakes_the_engine(self, h):
        h.engine._wake.clear()
        server = engine_control.ControlServer(
            lambda: h.engine, host="auto", backup_root=h.switcher.backup_dir
        )
        reply = server.handle({"op": "wake"})
        assert reply["result"] == "woken" and engine_control.exit_code(reply) == 0
        assert h.engine._wake.is_set()

    def test_fake_engine_without_fleet_state(self, tmp_path):
        from tests.test_engine_control import FakeEngine

        server = engine_control.ControlServer(
            lambda: FakeEngine(), host="auto", backup_root=tmp_path
        )
        assert server.handle({"op": "status"})["fleet"]["role"] == "none"
