"""Fork (cswap fleet): the TUI Hosts screen (``tui/fleetview.py``).

Every fleet_remote entry point the screen calls is stubbed: no ssh, no engine
IPC, no hosts.json writes. The app runs headlessly against the FakeSwitcher
from ``test_tui``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from claude_swap import fleet_remote
from claude_swap.settings import SELECTABLE_STRATEGIES
from claude_swap.tui import fleetview

from tests.test_tui import FakeSwitcher, make_account, make_app, settle

HOST = fleet_remote.Host(name="w1", alias="w1-alias")
HOST2 = fleet_remote.Host(name="w2", alias="w2-alias")


def _entry(name="w1", **over) -> dict:
    entry = {
        "name": name,
        "alias": f"{name}-alias",
        "local": False,
        "source": "leader",
        "reachable": True,
        "error": None,
        "state": "following",
        "reason": None,
        "lease_age_s": 12.0,
        "active": "2",
        "relogin": ["4"],
        "drift": True,
        "override": {"mode": "follow", "account": None, "strategy": None},
    }
    entry.update(over)
    return entry


def _doc(*entries) -> dict:
    local = {"name": "local", "local": True, "reachable": True, "state": "leading"}
    return {"contract": "cswap-fleet/1", "leader": {"live": True}, "hosts": [local, *entries]}


class Stubs:
    def __init__(self) -> None:
        self.status_calls: list[dict] = []
        self.override_calls: list[tuple] = []
        self.push_calls: list[tuple] = []
        self.doc = _doc(_entry(), _entry("w2", state="autonomous", reason="no lease",
                                         reachable=True, relogin=[], drift=False,
                                         lease_age_s=None, active="1"))
        self.leader_snap: dict | None = None
        self.leader = None
        self.hosts = [HOST, HOST2]
        self.push_result = [
            {"name": "w1", "ok": True, "error": None, "changed": True,
             "settings": [{"key": "autoswitch.threshold", "from": 90.0, "to": 85.0}],
             "accounts": [{"account": "3", "field": "disabled", "from": False, "to": True}],
             "skipped": []}
        ]
        self.status_gate: threading.Event | None = None
        self.status_entered = threading.Event()


@pytest.fixture
def stubs(monkeypatch):
    s = Stubs()

    def fleet_status(switcher, backup_root, *, leader_snapshot, runner=None, timeout=None):
        s.status_calls.append({"leader_snapshot": leader_snapshot, "runner": runner})
        s.status_entered.set()
        if s.status_gate is not None:
            s.status_gate.wait(timeout=5)
        return {**s.doc, "hosts": [dict(h) for h in s.doc["hosts"]]}

    def set_override(backup_root, name, override):
        s.override_calls.append((backup_root, name, override))
        normalized = fleet_remote.normalize_override(override)
        return fleet_remote.Host(name, f"{name}-alias", "cswap", normalized)

    def push(switcher, hosts, *, dry_run, **_kw):
        s.push_calls.append((switcher, list(hosts), dry_run))
        return s.push_result

    monkeypatch.setattr(fleet_remote, "fleet_status", fleet_status)
    monkeypatch.setattr(fleet_remote, "set_override", set_override)
    monkeypatch.setattr(fleet_remote, "push", push)
    monkeypatch.setattr(fleet_remote, "load_hosts", lambda root, **kw: list(s.hosts))
    monkeypatch.setattr(fleetview, "leader_snapshot", lambda root, leader=None: s.leader_snap)
    monkeypatch.setattr(fleetview, "inprocess_leader", lambda app: s.leader)
    return s


class FakeLeader:
    def __init__(self) -> None:
        self.kicks = 0

    def kick(self) -> None:
        self.kicks += 1

    def snapshot(self) -> dict:
        return {"leader_id": "x", "hosts": {}}


def _fake(tmp_path) -> FakeSwitcher:
    return FakeSwitcher(
        [make_account(1), make_account(2, active=True), make_account(3)], tmp_path
    )


def _rows(app) -> list[str]:
    from textual.widgets import OptionList

    options = app.screen.query_one("#fleet-hosts", OptionList)
    return [
        options.get_option_at_index(i).prompt.plain for i in range(options.option_count)
    ]


async def _open(pilot) -> None:
    await settle(pilot)
    await pilot.press("h")
    await pilot.pause()
    await settle(pilot)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_describe_override(self):
        assert fleetview.describe_override(None) == "follow"
        assert fleetview.describe_override({"mode": "pin", "account": "3"}) == "pin 3"
        assert (
            fleetview.describe_override({"mode": "strategy", "strategy": "best"})
            == "strategy best"
        )

    def test_format_push_result(self):
        body = fleetview.format_push_result(Stubs().push_result)
        assert "autoswitch.threshold: 90.0 -> 85.0" in body
        assert "account 3 disabled: False -> True" in body
        same = fleetview.format_push_result([{"name": "w1", "ok": True}])
        assert "nothing changed" in same
        failed = fleetview.format_push_result([{"name": "w1", "ok": False, "error": "timeout"}])
        assert "push failed: timeout" in failed


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRoute:
    async def test_no_hosts_no_binding(self, tmp_path):
        app = make_app(_fake(tmp_path))  # real load_hosts: no hosts.json -> []
        async with app.run_test(size=(120, 36)) as pilot:
            await settle(pilot)
            assert app.check_action("open_hosts", ()) is False
            keys = {b.binding.key for b in app.screen.active_bindings.values() if b.enabled}
            assert "h" not in keys
            await pilot.press("h")
            await pilot.pause()
            assert not isinstance(app.screen, fleetview.HostsScreen)

    async def test_hosts_configured_opens_screen(self, tmp_path, stubs):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(120, 36)) as pilot:
            await settle(pilot)
            assert app.check_action("open_hosts", ()) is True
            await _open(pilot)
            assert isinstance(app.screen, fleetview.HostsScreen)
            # not on the Hosts screen itself
            assert app.check_action("open_hosts", ()) is False
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, fleetview.HostsScreen)


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHostsScreen:
    async def test_renders_rows_not_leading(self, tmp_path, stubs):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            # not leading: one ssh pull on open
            assert stubs.status_calls[0]["leader_snapshot"] is None
            rows = _rows(app)
            assert len(rows) == 2  # workers only, the local row is the header's
            w1, w2 = rows
            assert w1.startswith("w1") and "following" in w1 and "active 2" in w1
            assert "lease 12s" in w1 and "re-login 4" in w1 and "settings drift" in w1
            assert "autonomous (no lease)" in w2 and "no lease" in w2 and "in sync" in w2
            assert all("@" not in r for r in rows)
            from textual.widgets import Static

            badge = app.screen.query_one("#fleet-badge", Static).render().plain
            header = app.screen.query_one("#fleet-header", Static).render().plain
            assert "NOT LEADING" in badge
            assert "apply only while this box leads" in header

    async def test_unreachable_and_override_rows(self, tmp_path, stubs):
        stubs.doc = _doc(
            _entry(reachable=False, error="timeout after 20s"),
            _entry("w2", state="overridden",
                   override={"mode": "pin", "account": "3", "strategy": None}),
        )
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            w1, w2 = _rows(app)
            assert "unreachable" in w1 and "timeout after 20s" in w1
            assert "overridden: pin 3" in w2

    async def test_leading_uses_leader_snapshot_without_ssh(self, tmp_path, stubs):
        stubs.leader_snap = {"leader_id": "abc", "hosts": {}}
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            call = stubs.status_calls[0]
            assert call["leader_snapshot"] == stubs.leader_snap
            assert call["runner"] is fleetview._no_ssh_runner
            from textual.widgets import Static

            assert "LEADING" in app.screen.query_one("#fleet-badge", Static).render().plain
            # the timer re-reads the leader, still without ssh
            app.screen._on_timer()
            await settle(pilot)
            assert len(stubs.status_calls) == 2
            assert stubs.status_calls[1]["runner"] is fleetview._no_ssh_runner

    async def test_timer_never_sshes_when_not_leading(self, tmp_path, stubs):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            assert len(stubs.status_calls) == 1
            app.screen._on_timer()
            await settle(pilot)
            assert len(stubs.status_calls) == 1
            assert len(_rows(app)) == 2  # last pull stays on screen

    async def test_refresh_pulls_over_ssh(self, tmp_path, stubs):
        stubs.leader_snap = {"leader_id": "abc", "hosts": {}}
        stubs.leader = FakeLeader()
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            await pilot.press("r")
            await settle(pilot)
            assert stubs.status_calls[-1]["leader_snapshot"] is None
            assert stubs.status_calls[-1]["runner"] is None  # default runner = real ssh
            assert stubs.leader.kicks == 1

    async def test_follow_key(self, tmp_path, stubs):
        stubs.leader = FakeLeader()
        stubs.leader_snap = {"leader_id": "abc", "hosts": {}}
        fake = _fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            await pilot.press("f")
            await settle(pilot)
            assert stubs.override_calls == [(tmp_path, "w1", {"mode": "follow"})]
            assert stubs.leader.kicks == 1

    async def test_strategy_key_cycles(self, tmp_path, stubs):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            await pilot.press("s")
            await settle(pilot)
            await pilot.press("s")
            await settle(pilot)
            first = SELECTABLE_STRATEGIES[0]
            second = SELECTABLE_STRATEGIES[1 % len(SELECTABLE_STRATEGIES)]
            assert [c[2] for c in stubs.override_calls] == [
                {"mode": "strategy", "strategy": first},
                {"mode": "strategy", "strategy": second},
            ]
            assert "override strategy " + second in _rows(app)[0]

    async def test_pin_key_on_second_row(self, tmp_path, stubs):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            await pilot.press("down")
            await pilot.press("p")
            await pilot.pause()
            from claude_swap.tui.modals import AccountPickModal

            assert isinstance(app.screen, AccountPickModal)
            await pilot.press("3")
            await settle(pilot)
            assert stubs.override_calls == [
                (tmp_path, "w2", {"mode": "pin", "account": "3"})
            ]

    async def test_pin_rejects_unmanaged_slot(self, tmp_path, stubs):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            app.screen._on_pin("w1", "9")
            await settle(pilot)
            assert stubs.override_calls == []

    async def test_push_key(self, tmp_path, stubs):
        fake = _fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(140, 36)) as pilot:
            await _open(pilot)
            await pilot.press("u")
            await pilot.pause()
            from claude_swap.tui.modals import ConfirmModal, OutputModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await settle(pilot)
            assert stubs.push_calls == [(fake, [HOST], False)]
            assert isinstance(app.screen, OutputModal)
            assert "autoswitch.threshold: 90.0 -> 85.0" in app.screen._output

    async def test_slow_status_does_not_block_ui(self, tmp_path, stubs):
        stubs.status_gate = threading.Event()
        app = make_app(_fake(tmp_path))
        try:
            async with app.run_test(size=(140, 36)) as pilot:
                await settle(pilot)
                await pilot.press("h")
                assert await asyncio.to_thread(stubs.status_entered.wait, 2)
                await pilot.pause()
                assert isinstance(app.screen, fleetview.HostsScreen)
                assert "loading" in _rows(app)[0]
                # the UI still answers keys while fleet_status sits on ssh
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, fleetview.HostsScreen)
                stubs.status_gate.set()
                await settle(pilot)
        finally:
            stubs.status_gate.set()
