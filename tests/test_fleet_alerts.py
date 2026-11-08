"""Fork (fleet): needs-login alerts (fleet_alerts.py, contract cswap-fleet-alert/1).

No real subprocess: every command goes through an injected fake runner.
Synthetic stores only (tmp_path / temp_home).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import fleet_alerts as fa
from claude_swap import fleet_lease
from claude_swap import fleet_remote as fr
from claude_swap.settings import (
    SETTING_SPECS,
    FleetSettings,
    load_fleet_settings,
    set_setting,
    settings_path,
)

from tests.test_account_policy_store import PolicyStoreBase

T0 = 1_800_000_000.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


class FakeRunner:
    """subprocess.run stand-in: records argv/kwargs/stdin; ``rc`` per call;
    ``block`` makes a call wait until released."""

    def __init__(self, rc=0, block=False):
        self.rc = rc
        self.calls: list[tuple[list, dict]] = []
        self.block = block
        self.release = threading.Event()
        self.entered = threading.Event()

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        self.entered.set()
        if self.block:
            self.release.wait(10)
        rc = self.rc(len(self.calls)) if callable(self.rc) else self.rc
        if isinstance(rc, BaseException):
            raise rc
        return SimpleNamespace(returncode=rc, stdout="sent to someone@example.test", stderr="")

    def payloads(self):
        return [json.loads(kw["input"]) for _, kw in self.calls]


def _configure(root: Path, command: str = "sender --mode fleet") -> None:
    set_setting(root, "fleet.alertCommand", command)


# --------------------------------------------------------------------------
# Command-line splitting
# --------------------------------------------------------------------------


class TestSplitCommand:
    @pytest.mark.parametrize("command,argv", [
        (r'"C:\Program Files\Python313\python.exe" "D:\some dir\send_alert.py"',
         [r"C:\Program Files\Python313\python.exe", r"D:\some dir\send_alert.py"]),
        # Forward slashes and a space in the script's directory: exactly 2 items.
        ('"C:/Program Files/Python313/python.exe" "D:/Example Dir/tools/send_alert.py"',
         ["C:/Program Files/Python313/python.exe", "D:/Example Dir/tools/send_alert.py"]),
        (r"C:\Python313\python.exe D:\tools\send.py --to ops",
         [r"C:\Python313\python.exe", r"D:\tools\send.py", "--to", "ops"]),
        (r'"C:\a b\x.exe" --flag "D:\dir with space\\" tail',
         [r"C:\a b\x.exe", "--flag", "D:\\dir with space\\", "tail"]),
        (r'prog a\\b "say \"hi\"" ""', ["prog", r"a\\b", 'say "hi"', ""]),
        (r'prog "a ""quoted"" word"', ["prog", 'a "quoted" word']),
        ("   ", []),
    ])
    def test_windows(self, command, argv):
        assert fa.split_command(command, windows=True) == argv

    @pytest.mark.parametrize("command", [r'"C:\no end\x.exe', r'prog "open', r'"C:\x.exe"tail'])
    def test_windows_bad(self, command):
        with pytest.raises(ValueError):
            fa.split_command(command, windows=True)

    def test_posix(self):
        assert fa.split_command("/usr/bin/python3 '/opt/my dir/send.py' --x \"y z\"",
                                windows=False) == [
            "/usr/bin/python3", "/opt/my dir/send.py", "--x", "y z"]
        with pytest.raises(ValueError):
            fa.split_command("prog 'open", windows=False)

    def test_platform_default(self, monkeypatch):
        cmd = r'"C:\Program Files\p.exe" D:\x\y.py'
        monkeypatch.setattr(fa.sys, "platform", "win32")
        assert fa.split_command(cmd) == [r"C:\Program Files\p.exe", r"D:\x\y.py"]
        monkeypatch.setattr(fa.sys, "platform", "linux")
        assert fa.split_command(cmd) == [r"C:\Program Files\p.exe", "D:xy.py"]  # shlex rules


# --------------------------------------------------------------------------
# Setting
# --------------------------------------------------------------------------


class TestSetting:
    def test_spec_and_load(self, tmp_path):
        spec = SETTING_SPECS["fleet.alertCommand"]
        assert spec.kind == "string" and spec.default == ""
        assert load_fleet_settings(tmp_path) == FleetSettings()
        _configure(tmp_path, r'"C:\Program Files\p.exe" "D:\a b\s.py"')
        loaded = load_fleet_settings(tmp_path)
        assert loaded.alert_command == r'"C:\Program Files\p.exe" "D:\a b\s.py"'
        assert loaded.fallback_strategy == FleetSettings().fallback_strategy
        assert fa.alert_command(tmp_path) == loaded.alert_command

    def test_non_string_is_off_and_keeps_fallback(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps(
            {"fleet": {"alertCommand": 5, "fallbackStrategy": "best"}}))
        assert load_fleet_settings(tmp_path) == FleetSettings(fallback_strategy="best")
        assert fa.alert_command(tmp_path) == ""

    def test_not_pushed_nor_fingerprinted(self, temp_home):
        s = PolicyStoreBase()._fleet(temp_home)
        before = fleet_lease.settings_fingerprint(s)
        _configure(s.backup_dir, "sender --secret-flag")
        assert fleet_lease.settings_fingerprint(s) == before
        assert "alertCommand" not in json.dumps(fleet_lease.fingerprint_document(s))
        doc = fr.export_settings(s)
        assert not any("alert" in k for k in doc["settings"])
        assert "sender" not in json.dumps(doc)
        doc["settings"]["fleet.alertCommand"] = "evil"
        out = fr.apply_settings(s, doc)
        assert "fleet.alertCommand" in out["ignored"]
        assert fa.alert_command(s.backup_dir) == "sender --secret-flag"


# --------------------------------------------------------------------------
# relogin_detail
# --------------------------------------------------------------------------


class TestReloginDetail:
    def test_reasons_match_relogin_slots(self, tmp_path):
        (tmp_path / "autoswitch_state.json").write_text(json.dumps({
            "quarantine": {"4": {"reason": "invalid_grant"}, "2": {}},
        }), encoding="utf-8")
        payload = {"accounts": [
            {"number": 1, "usageStatus": "ok", "email": "a@example.test"},
            {"number": 2, "usageStatus": "relogin_required", "loginExpiresAt": _iso(T0 + 99 * 86400)},
            {"number": 3, "usageStatus": "ok", "loginExpiresAt": _iso(T0 + 6 * 86400)},
            {"number": 5, "usageStatus": "ok", "loginExpiresAt": _iso(T0 + 8 * 86400)},
            {"number": 6, "usageStatus": "ok", "loginExpiresAt": "2027-01-01T00:00:00.123+00:00"},
            {"number": 7, "usageStatus": "ok", "loginExpiresAt": "garbage"},
        ]}
        detail = fleet_lease.relogin_detail(payload, tmp_path, now=T0 + 400 * 86400)
        assert [d["slot"] for d in detail] == fleet_lease.relogin_slots(
            payload, tmp_path, now=T0 + 400 * 86400)
        detail = fleet_lease.relogin_detail(payload, tmp_path, now=T0)
        assert detail == [
            {"slot": "2", "reason": "relogin_required", "expires_at": _iso(T0 + 99 * 86400)},
            {"slot": "3", "reason": "expiring", "expires_at": _iso(T0 + 6 * 86400)},
            {"slot": "4", "reason": "quarantined", "expires_at": None},
            # T0 is mid-January 2027: already expired, normalized to seconds + Z.
            {"slot": "6", "reason": "expiring", "expires_at": "2027-01-01T00:00:00Z"},
        ]
        assert [d["slot"] for d in detail] == fleet_lease.relogin_slots(payload, tmp_path, now=T0)
        assert "@" not in json.dumps(detail)

    def test_older_worker_snapshot_derives_reasons(self):
        snap = {
            "host_time": T0,
            "accounts": {"accounts": [
                {"number": 1, "usageStatus": "relogin_required", "email": "x@example.test"},
                {"number": 2, "usageStatus": "ok", "loginExpiresAt": _iso(T0 + 86400)},
            ]},
            "relogin": ["1", "2", "3"],
        }
        assert fa.detail_from_snapshot(snap) == [
            {"slot": "1", "reason": "relogin_required", "expires_at": None},
            {"slot": "2", "reason": "expiring", "expires_at": _iso(T0 + 86400)},
            {"slot": "3", "reason": "quarantined", "expires_at": None},
        ]
        assert fa.detail_from_snapshot({"host": "x"}) is None
        assert fa.detail_from_snapshot({"accounts": {"accounts": [], "error": "list-failed"},
                                        "relogin": [], "relogin_detail": []}) is None
        snap["relogin_detail"] = [{"slot": "9", "reason": "expiring", "expires_at": None},
                                  {"slot": "x", "reason": "expiring"},
                                  {"slot": "8", "reason": "bogus"}]
        assert fa.detail_from_snapshot(snap) == [
            {"slot": "9", "reason": "expiring", "expires_at": None}]


# --------------------------------------------------------------------------
# Event computation
# --------------------------------------------------------------------------


def _d(slot, reason="expiring", expires_at=None):
    return {"slot": slot, "reason": reason, "expires_at": expires_at}


class TestComputeEvents:
    def test_new_then_deduped_then_cleared(self):
        state = fa._empty_state()
        ev = fa.compute_events(state, relogin={"local": [_d("3", "relogin_required")],
                                               "w1": [_d("2", "expiring", "2026-11-10T00:00:00Z")]},
                               down={}, up={"local", "w1"})
        assert ev == [
            {"kind": "relogin", "host": "local", "slot": "3", "reason": "relogin_required",
             "expires_at": None},
            {"kind": "relogin", "host": "w1", "slot": "2", "reason": "expiring",
             "expires_at": "2026-11-10T00:00:00Z"},
        ]
        state = fa.apply_events(state, ev, at="T1")
        assert fa.compute_events(state, relogin={"local": [_d("3", "relogin_required")],
                                                 "w1": [_d("2", "expiring", "2026-11-10T00:00:00Z")]},
                                 down={}, up={"local", "w1"}) == []
        # w1's slot cleared; local's reason changed: a new relogin event.
        ev = fa.compute_events(state, relogin={"local": [_d("3", "quarantined")], "w1": []},
                               down={}, up={"local", "w1"})
        assert ev == [
            {"kind": "relogin", "host": "local", "slot": "3", "reason": "quarantined",
             "expires_at": None},
            {"kind": "relogin-cleared", "host": "w1", "slot": "2", "reason": "expiring",
             "since": "T1"},
        ]
        state = fa.apply_events(state, ev, at="T2")
        assert state["relogin"] == {"local": {"3": {"reason": "quarantined", "expires_at": None,
                                                    "since": "T2"}}}

    def test_new_expiry_realerts(self):
        state = fa.apply_events(fa._empty_state(), [
            {"kind": "relogin", "host": "w1", "slot": "2", "reason": "expiring",
             "expires_at": "A"}], at="T1")
        ev = fa.compute_events(state, relogin={"w1": [_d("2", "expiring", "B")]}, down={}, up=set())
        assert [e["expires_at"] for e in ev] == ["B"]

    def test_unreachable_host_keeps_its_slots(self):
        state = fa.apply_events(fa._empty_state(), [
            {"kind": "relogin", "host": "w1", "slot": "2", "reason": "expiring",
             "expires_at": None}], at="T1")
        ev = fa.compute_events(state, relogin={}, down={"w1": "S"}, up=set())
        assert ev == [{"kind": "unreachable", "host": "w1", "since": "S"}]
        state = fa.apply_events(state, ev, at="T2")
        assert fa.compute_events(state, relogin={}, down={"w1": "S"}, up=set()) == []
        assert state["relogin"]["w1"]["2"]["reason"] == "expiring"
        ev = fa.compute_events(state, relogin={"w1": [_d("2")]}, down={}, up={"w1"})
        assert ev == [{"kind": "reachable", "host": "w1", "since": "S"}]
        state = fa.apply_events(state, ev, at="T3")
        assert state["unreachable"] == {}

    def test_removed_hosts_are_pruned(self):
        state = fa.apply_events(fa._empty_state(), [
            {"kind": "relogin", "host": "gone", "slot": "1", "reason": "expiring",
             "expires_at": None},
            {"kind": "unreachable", "host": "gone2", "since": "S"}], at="T1",
            known_hosts={"local"})
        assert state["relogin"] == {} and state["unreachable"] == {}


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------


VIEW = dict(relogin={"local": [_d("3", "relogin_required")]}, down={"w2": "2026-11-07T00:00:00Z"},
            up={"local"}, known_hosts={"local", "w2"})


class TestDispatcher:
    def test_off_without_command(self, tmp_path):
        runner = FakeRunner()
        d = fa.AlertDispatcher(tmp_path, runner=runner)
        assert d.submit(**VIEW) is False
        assert runner.calls == [] and not fa.alerts_path(tmp_path).exists()

    def test_payload_and_dedup_across_restart(self, tmp_path):
        _configure(tmp_path, r'"C:\Program Files\p.exe" "D:\a b\send.py"')
        runner = FakeRunner()
        d = fa.AlertDispatcher(tmp_path, runner=runner, clock=lambda: T0)
        assert d.submit(**VIEW) is True
        d.join(5)
        assert len(runner.calls) == 1
        argv, kw = runner.calls[0]
        assert argv[0].endswith("p.exe") and kw["shell"] is False and kw["timeout"] == 30.0
        assert runner.payloads()[0] == {
            "contract": "cswap-fleet-alert/1",
            "generated_at": _iso(T0),
            "events": [
                {"kind": "unreachable", "host": "w2", "since": "2026-11-07T00:00:00Z"},
                {"kind": "relogin", "host": "local", "slot": "3",
                 "reason": "relogin_required", "expires_at": None},
            ],
        }
        stored = json.loads(fa.alerts_path(tmp_path).read_text())
        assert stored["relogin"]["local"]["3"]["reason"] == "relogin_required"
        assert "w2" in stored["unreachable"]
        # Same view again, and after a "restart" (a fresh dispatcher): silent.
        assert d.submit(**VIEW) is False
        assert fa.AlertDispatcher(tmp_path, runner=runner).submit(**VIEW) is False
        assert len(runner.calls) == 1

    def test_nonzero_exit_retries_next_round(self, tmp_path, caplog):
        _configure(tmp_path)
        runner = FakeRunner(rc=lambda n: 1 if n == 1 else 0)
        d = fa.AlertDispatcher(tmp_path, runner=runner)
        with caplog.at_level("DEBUG", logger="claude-swap"):
            assert d.submit(**VIEW) is True
            d.join(5)
        assert not fa.alerts_path(tmp_path).exists()
        assert "exit 1" in caplog.text
        for leaked in ("w2", "relogin_required", "example.test", "--mode"):
            assert leaked not in caplog.text
        assert d.submit(**VIEW) is True
        d.join(5)
        assert len(runner.calls) == 2
        assert runner.payloads()[0]["events"] == runner.payloads()[1]["events"]
        assert fa.alerts_path(tmp_path).exists()

    @pytest.mark.parametrize("exc,msg", [
        (subprocess.TimeoutExpired("x", 30), "timeout after 30s"),
        (FileNotFoundError(), "command not found"),
        (OSError("boom"), "failed to start (OSError)"),
    ])
    def test_run_alert_never_raises(self, exc, msg):
        rc, error = fa.run_alert(["x"], {"events": []}, runner=FakeRunner(rc=exc))
        assert rc is None and error == msg

    def test_hung_command_never_blocks_and_one_in_flight(self, tmp_path):
        _configure(tmp_path)
        runner = FakeRunner(block=True)
        d = fa.AlertDispatcher(tmp_path, runner=runner)
        try:
            start = time.monotonic()
            assert d.submit(**VIEW) is True
            assert time.monotonic() - start < 2
            assert runner.entered.wait(5)
            assert d.in_flight
            assert d.submit(**VIEW) is False  # at most one command in flight
            assert len(runner.calls) == 1
        finally:
            runner.release.set()
            d.join(5)
        assert not d.in_flight

    def test_unsplittable_command_is_skipped(self, tmp_path):
        _configure(tmp_path, '"unterminated')
        runner = FakeRunner()
        assert fa.AlertDispatcher(tmp_path, runner=runner).submit(**VIEW) is False
        assert runner.calls == []


# --------------------------------------------------------------------------
# cswap fleet alert-test
# --------------------------------------------------------------------------


def _run(capsys, argv, **kw):
    rc = fr.run(argv, **kw)
    return rc, capsys.readouterr().out


class TestAlertTestVerb:
    def test_no_command(self, tmp_path, capsys):
        runner = FakeRunner()
        rc, out = _run(capsys, ["alert-test", "--json"], backup_root=tmp_path, runner=runner)
        doc = json.loads(out)
        assert rc == 1 and doc["op"] == "alert-test" and doc["error"] == "no-alert-command"
        assert runner.calls == []
        rc, out = _run(capsys, ["alert-test"], backup_root=tmp_path, runner=runner)
        assert rc == 1 and out.startswith("error (no-alert-command)")

    def test_sends_one_test_event(self, tmp_path, capsys):
        _configure(tmp_path, r'"C:\Program Files\p.exe" "D:\a b\send.py"')
        runner = FakeRunner()
        rc, out = _run(capsys, ["alert-test", "--json"], backup_root=tmp_path, runner=runner)
        doc = json.loads(out)
        assert rc == 0 and doc["result"] == "sent" and doc["exit_code"] == 0
        assert doc["event"] == {"kind": "test", "host": "local"}
        (payload,) = runner.payloads()
        assert payload["contract"] == "cswap-fleet-alert/1"
        assert payload["events"] == [{"kind": "test", "host": "local"}]
        assert not fa.alerts_path(tmp_path).exists()  # a test is never recorded
        rc, out = _run(capsys, ["alert-test"], backup_root=tmp_path, runner=runner)
        assert rc == 0 and "exited 0" in out

    def test_failure_reports_exit_code(self, tmp_path, capsys):
        _configure(tmp_path)
        rc, out = _run(capsys, ["--json", "alert-test"], backup_root=tmp_path,
                       runner=FakeRunner(rc=3))
        doc = json.loads(out)
        assert rc == 1 and doc["error"] == "alert-failed" and doc["exit_code"] == 3
