"""`cswap fleet`, dev box side (fleet_remote.py): hosts config, the ssh
transport, settings push/apply, the `fleet status` router contract,
overrides, and the CLI verbs.

`fleet_lease` is stubbed throughout (`fleet_remote._lease`), so these tests
pin this module's contract, not the lease implementation. No real ssh: every
transport call goes through a fake runner. Synthetic stores only.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap import fleet_lease as real_lease
from claude_swap import fleet_remote as fr
from claude_swap.settings import (
    SETTING_SPECS,
    SELECTABLE_STRATEGIES,
    FleetSettings,
    load_fleet_settings,
    load_settings,
    set_setting,
    settings_path,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.exceptions import ConfigError

from tests.test_account_policy_store import PolicyStoreBase


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------


def _snap(active=2, state="following", settings_hash="fp-local", relogin=()):
    return {
        "contract": fr.CONTRACT,
        "host_time": 0,
        "accounts": {
            "schemaVersion": 1,
            "activeAccountNumber": active,
            "accounts": [
                {
                    "number": 1,
                    "email": "account-1@example.test",
                    "organizationName": "Org",
                    "organizationUuid": "org-1",
                    "active": active == 1,
                    "usageStatus": "ok",
                    "usage": None,
                    "alias": "owner account-1@example.test",
                },
                {
                    "number": 2,
                    "email": "account-2@example.test",
                    "organizationName": "",
                    "organizationUuid": "",
                    "active": active == 2,
                    "usageStatus": "ok",
                    "usage": None,
                },
            ],
            "duplicateAccountWarnings": ["account-1@example.test"],
        },
        "engine": {"result": "running", "engine": {"active": str(active), "mode": "auto"}},
        "follow": {"state": state, "reason": None, "lease_age_s": 12.0},
        "relogin": list(relogin),
        "settings_hash": settings_hash,
    }


class StubLease:
    CONTRACT = fr.CONTRACT

    def __init__(self):
        self.local = _snap(active=2, state="autonomous")
        self.fingerprint = "fp-local"
        self.received = []
        self.receive_error = None

    @staticmethod
    def identity_hash(email, org_uuid, salt):
        material = "\x00".join((salt, (email or "").strip().lower(), org_uuid or ""))
        return hashlib.sha256(material.encode()).hexdigest()

    # The settings document is the drift fingerprint's own key set: use the
    # real producer so export/apply stay pinned to it.
    fingerprint_document = staticmethod(real_lease.fingerprint_document)

    def build_snapshot(self, switcher, backup_root, *, now=None):
        return self.local

    def settings_fingerprint(self, switcher):
        return self.fingerprint

    def receive_lease(self, switcher, payload, *, now=None):
        if self.receive_error:
            raise self.receive_error
        self.received.append(payload)
        return {"contract": fr.CONTRACT, "follow": {"state": "following"}}


@pytest.fixture
def lease(monkeypatch):
    stub = StubLease()
    monkeypatch.setattr(fr, "_lease", lambda: stub)
    return stub


class FakeRunner:
    """subprocess.run stand-in keyed by ssh alias (argv[5])."""

    def __init__(self, replies=None, default=None):
        self.replies = replies or {}
        self.default = default
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        reply = self.replies.get(argv[5], self.default)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(argv, kwargs)
        rc, out, err = reply
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _ok(obj):
    return (0, json.dumps(obj) + "\n", "")


class FakeSwitcher:
    def __init__(self, root: Path):
        self.backup_dir = root


# --------------------------------------------------------------------------
# Hosts config
# --------------------------------------------------------------------------


class TestHosts:
    def test_add_list_remove_round_trip(self, tmp_path):
        fr.add_host(tmp_path, "worker-a")
        fr.add_host(tmp_path, "user@worker-b", name="worker-b", command="python3 -m claude_swap")
        hosts = fr.load_hosts(tmp_path)
        assert [h.name for h in hosts] == ["worker-a", "worker-b"]
        assert hosts[0].command == "cswap"
        assert hosts[0].override == fr.DEFAULT_OVERRIDE
        assert hosts[1].alias == "user@worker-b"
        raw = json.loads(fr.hosts_path(tmp_path).read_text())
        assert raw["contract"] == fr.CONTRACT
        fr.remove_host(tmp_path, "worker-a")
        assert [h.name for h in fr.load_hosts(tmp_path)] == ["worker-b"]

    def test_remove_by_alias(self, tmp_path):
        fr.add_host(tmp_path, "user@worker-b", name="worker-b")
        fr.remove_host(tmp_path, "user@worker-b")
        assert fr.load_hosts(tmp_path) == []

    def test_duplicate_name_rejected(self, tmp_path):
        fr.add_host(tmp_path, "worker-a")
        with pytest.raises(fr.FleetError) as e:
            fr.add_host(tmp_path, "worker-a")
        assert e.value.code == "duplicate-host"

    @pytest.mark.parametrize(
        "alias",
        ["-oProxyCommand=x", "worker a", "worker;id", "$(id)", "w`id`", "w|x", "", "w\nx", "w'x"],
    )
    def test_bad_alias_rejected(self, tmp_path, alias):
        with pytest.raises(fr.FleetError):
            fr.add_host(tmp_path, alias)
        assert not fr.hosts_path(tmp_path).exists()

    @pytest.mark.parametrize(
        "command",
        ["cswap; id", "cswap && id", "'/path with space/cswap'", "cswap $(id)", "", "cswap |x", '"unterminated'],
    )
    def test_bad_command_rejected(self, tmp_path, command):
        with pytest.raises(fr.FleetError):
            fr.add_host(tmp_path, "worker-a", command=command)

    @pytest.mark.parametrize("command", ["cswap", "~/.local/bin/cswap", "python3 -m claude_swap"])
    def test_good_commands(self, tmp_path, command):
        assert fr.add_host(tmp_path, "worker-a", command=command).command == command

    def test_local_name_reserved(self, tmp_path):
        with pytest.raises(fr.FleetError):
            fr.add_host(tmp_path, "worker-a", name="local")

    def test_missing_file_is_empty(self, tmp_path):
        assert fr.load_hosts(tmp_path) == []

    def test_corrupt_file_lenient_vs_strict(self, tmp_path):
        path = fr.hosts_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert fr.load_hosts(tmp_path) == []
        with pytest.raises(fr.FleetError):
            fr.load_hosts(tmp_path, strict=True)
        with pytest.raises(fr.FleetError):
            fr.add_host(tmp_path, "worker-a")
        assert path.read_text() == "{not json"  # never overwritten

    def test_invalid_entry_skipped_when_lenient(self, tmp_path):
        path = fr.hosts_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hosts": [
            {"alias": "worker-a"}, {"alias": "bad alias"}, {"alias": "worker-a"},
        ]}))
        assert [h.name for h in fr.load_hosts(tmp_path)] == ["worker-a"]


class TestOverride:
    def test_pin_strategy_follow(self, tmp_path):
        fr.add_host(tmp_path, "worker-a")
        fr.set_override(tmp_path, "worker-a", {"mode": "pin", "account": "03"})
        assert fr.load_hosts(tmp_path)[0].override == {
            "mode": "pin", "account": "3", "strategy": None}
        fr.set_override(tmp_path, "worker-a", {"mode": "strategy", "strategy": "consume-first"})
        assert fr.load_hosts(tmp_path)[0].override["strategy"] == "consume-first"
        fr.set_override(tmp_path, "worker-a", {"mode": "follow"})
        assert fr.load_hosts(tmp_path)[0].override == fr.DEFAULT_OVERRIDE

    @pytest.mark.parametrize("override", [
        {"mode": "pin", "account": "0"},
        {"mode": "pin", "account": "x"},
        {"mode": "strategy", "strategy": "priority-consume-first"},  # suppressed
        {"mode": "strategy", "strategy": "nope"},
        {"mode": "spread"},
    ])
    def test_invalid_rejected(self, tmp_path, override):
        fr.add_host(tmp_path, "worker-a")
        with pytest.raises(fr.FleetError):
            fr.set_override(tmp_path, "worker-a", override)

    def test_unknown_host(self, tmp_path):
        with pytest.raises(fr.FleetError) as e:
            fr.set_override(tmp_path, "worker-z", {"mode": "follow"})
        assert e.value.code == "unknown-host"


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


HOST = fr.Host("worker-a", "worker-a", "python3 -m claude_swap")


class TestRunRemote:
    def test_argv_and_stdin(self):
        runner = FakeRunner(default=_ok({"result": "ok"}))
        res = fr.run_remote(HOST, ["fleet", "receive", "--json", "a b;c"], {"k": "v"}, runner=runner)
        assert res == fr.RemoteResult(0, {"result": "ok"}, None)
        argv, kwargs = runner.calls[0]
        assert argv == [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "worker-a",
            "python3", "-m", "claude_swap", "fleet", "receive", "--json", "'a b;c'",
        ]
        assert json.loads(kwargs["input"]) == {"k": "v"}
        assert kwargs["timeout"] == fr.DEFAULT_TIMEOUT
        assert kwargs["capture_output"] is True

    def test_no_payload_sends_empty_stdin(self):
        runner = FakeRunner(default=_ok({}))
        fr.run_remote(HOST, ["fleet", "snapshot"], None, runner=runner)
        assert runner.calls[0][1]["input"] == ""

    def test_timeout(self):
        runner = FakeRunner(default=subprocess.TimeoutExpired(["ssh"], 3))
        res = fr.run_remote(HOST, [], None, timeout=3, runner=runner)
        assert res.rc is None and res.payload is None and "timeout" in res.error

    def test_ssh_missing(self):
        res = fr.run_remote(HOST, [], None, runner=FakeRunner(default=FileNotFoundError()))
        assert not res.ok and "ssh not found" in res.error

    def test_other_exception_never_raises(self):
        res = fr.run_remote(HOST, [], None, runner=FakeRunner(default=RuntimeError("x")))
        assert not res.ok

    def test_ssh_255(self):
        runner = FakeRunner(default=(255, "", "warn\nssh: connect to host worker-a: refused\n"))
        res = fr.run_remote(HOST, [], None, runner=runner)
        assert res.rc == 255 and res.payload is None
        assert "refused" in res.error

    def test_nonzero_keeps_payload(self):
        body = {"result": "error", "error": "invalid-lease", "message": "bad for a@example.test"}
        res = fr.run_remote(HOST, [], None, runner=FakeRunner(default=(1, json.dumps(body), "")))
        assert res.rc == 1 and res.payload == body
        assert "remote exit 1" in res.error and "@" not in res.error

    def test_bad_json(self):
        res = fr.run_remote(HOST, [], None, runner=FakeRunner(default=(0, "not json", "")))
        assert res.rc == 0 and res.payload is None and "JSON" in res.error

    def test_non_object_json(self):
        res = fr.run_remote(HOST, [], None, runner=FakeRunner(default=(0, "[1]", "")))
        assert not res.ok

    def test_noise_before_last_line_tolerated(self):
        res = fr.run_remote(HOST, [], None, runner=FakeRunner(default=(0, 'banner\n{"a": 1}\n', "")))
        assert res.payload == {"a": 1} and res.ok


# --------------------------------------------------------------------------
# Settings export / apply
# --------------------------------------------------------------------------


class TestApplySettings(PolicyStoreBase):
    def _store(self, temp_home):
        s = self._fleet(temp_home)
        return s

    def test_export_shape(self, temp_home, lease):
        s = self._store(temp_home)
        set_setting(s.backup_dir, "autoswitch.threshold", "80")
        set_setting(s.backup_dir, "ui.theme", "light")
        s.set_account_threshold("2", 70)
        doc = fr.export_settings(s)
        assert doc["contract"] == fr.CONTRACT and doc["kind"] == "settings"
        assert doc["settings"]["autoswitch.threshold"] == 80.0
        assert set(doc["settings"]) == {
            k for k, spec in SETTING_SPECS.items() if spec.section == "autoswitch"}
        assert "unset" not in doc
        assert set(doc["accounts"]) == {"1", "2", "3"}
        assert doc["accounts"]["2"]["threshold"] == 70.0
        assert "example.test" not in json.dumps(doc)

    def test_round_trip_mirrors_and_reports(self, temp_home, lease, capsys):
        s = self._store(temp_home)
        set_setting(s.backup_dir, "autoswitch.threshold", "80")
        s.set_account_threshold("2", 70)
        doc = fr.export_settings(s)
        # Drift the store, then apply the earlier document back.
        set_setting(s.backup_dir, "autoswitch.threshold", "85")
        set_setting(s.backup_dir, "autoswitch.strategy", "consume-first")
        s.set_account_threshold("2", None)
        s.set_account_disabled("3", True)
        capsys.readouterr()
        out = fr.apply_settings(s, doc)
        assert capsys.readouterr().out == ""  # setters' prints swallowed
        assert out["result"] == "applied" and out["changed"]
        assert {c["key"] for c in out["settings"]} == {
            "autoswitch.threshold", "autoswitch.strategy"}
        assert {(c["account"], c["field"]) for c in out["accounts"]} == {
            ("2", "threshold"), ("3", "disabled")}
        assert load_settings(s.backup_dir).threshold == 80.0
        raw = json.loads(settings_path(s.backup_dir).read_text())
        assert "strategy" not in raw["autoswitch"]  # unset, not frozen default
        assert s.account_policies()["2"].threshold == 70.0
        assert not s.is_account_disabled("3")
        again = fr.apply_settings(s, doc)
        assert not again["changed"]

    def test_apply_makes_fingerprints_equal(self, temp_home, lease):
        s = self._store(temp_home)
        set_setting(s.backup_dir, "autoswitch.threshold", "80")
        set_setting(s.backup_dir, "autoswitch.warmupEnabled", "true")
        s.set_account_disabled("2", True)
        want = real_lease.settings_fingerprint(s)
        doc = fr.export_settings(s)
        set_setting(s.backup_dir, "autoswitch.threshold", "90")  # explicit default
        set_setting(s.backup_dir, "autoswitch.intervalSeconds", "30")
        set_setting(s.backup_dir, "autoswitch.warmupEnabled", "false")
        s.set_account_disabled("2", False)
        s.set_account_priority("3", 5)
        assert real_lease.settings_fingerprint(s) != want
        fr.apply_settings(s, doc)
        assert real_lease.settings_fingerprint(s) == want

    def test_dry_run_writes_nothing(self, temp_home, lease):
        s = self._store(temp_home)
        set_setting(s.backup_dir, "autoswitch.threshold", "80")
        s.set_account_threshold("2", 70)
        doc = fr.export_settings(s)
        set_setting(s.backup_dir, "autoswitch.threshold", "85")
        s.set_account_threshold("2", None)
        before = (settings_path(s.backup_dir).read_bytes(), Path(s.sequence_file).read_bytes())
        out = fr.apply_settings(s, doc, dry_run=True)
        assert out["result"] == "dry-run" and out["changed"]
        after = (settings_path(s.backup_dir).read_bytes(), Path(s.sequence_file).read_bytes())
        assert before == after

    def test_identity_mismatch_and_missing_slot_skipped(self, temp_home, lease):
        s = self._store(temp_home)
        doc = fr.export_settings(s)
        doc["accounts"]["2"]["identity"] = "0" * 64
        doc["accounts"]["2"]["threshold"] = 60.0
        doc["accounts"]["9"] = {"identity": "x", "threshold": 60.0, "priority": 0, "disabled": False}
        before = Path(s.sequence_file).read_bytes()
        out = fr.apply_settings(s, doc)
        assert {"account": "2", "reason": "identity-mismatch"} in out["skipped"]
        assert {"account": "9", "reason": "missing"} in out["skipped"]
        assert Path(s.sequence_file).read_bytes() == before

    def test_box_local_and_unknown_keys_ignored(self, temp_home, lease):
        s = self._store(temp_home)
        doc = fr.export_settings(s)
        doc["settings"]["ui.theme"] = "light"
        doc["settings"]["fleet.fallbackStrategy"] = "best"
        doc["settings"]["bogus.key"] = 1
        out = fr.apply_settings(s, doc)
        assert set(out["ignored"]) == {"ui.theme", "fleet.fallbackStrategy", "bogus.key"}
        assert not settings_path(s.backup_dir).exists() or "ui" not in json.loads(
            settings_path(s.backup_dir).read_text())

    @pytest.mark.parametrize("mutate", [
        lambda d: d.update(contract="other/1"),
        lambda d: d.update(kind="lease"),
        lambda d: d.update(salt=""),
        lambda d: d.update(accounts={"x": {}}),
        lambda d: d["settings"].update({"autoswitch.threshold": 500}),
        lambda d: d["accounts"]["1"].update(threshold=5),
    ])
    def test_invalid_document_writes_nothing(self, temp_home, lease, mutate):
        s = self._store(temp_home)
        doc = fr.export_settings(s)
        mutate(doc)
        before = Path(s.sequence_file).read_bytes()
        with pytest.raises(fr.FleetError):
            fr.apply_settings(s, doc)
        assert Path(s.sequence_file).read_bytes() == before
        assert not settings_path(s.backup_dir).exists()


class TestPush(PolicyStoreBase):
    def test_push_sends_doc_and_collects(self, temp_home, lease):
        s = self._fleet(temp_home)
        reply = {"result": "applied", "changed": True, "settings": [{"key": "k"}],
                 "accounts": [], "skipped": [], "ignored": []}
        runner = FakeRunner({"worker-a": _ok(reply), "worker-b": (255, "", "refused")})
        hosts = [fr.Host("worker-a", "worker-a"), fr.Host("worker-b", "worker-b")]
        out = fr.push(s, hosts, dry_run=True, runner=runner)
        assert [o["name"] for o in out] == ["worker-a", "worker-b"]
        assert out[0]["ok"] and out[0]["changed"] and out[0]["settings"] == [{"key": "k"}]
        assert not out[1]["ok"] and "255" in out[1]["error"]
        for argv, kwargs in runner.calls:
            assert argv[6:] == ["cswap", "fleet", "apply", "--json", "--dry-run"]
            assert json.loads(kwargs["input"])["kind"] == "settings"

    def test_push_live_has_no_dry_run_flag(self, temp_home, lease):
        s = self._fleet(temp_home)
        runner = FakeRunner(default=_ok({"result": "applied"}))
        fr.push(s, [fr.Host("worker-a", "worker-a")], dry_run=False, runner=runner)
        assert "--dry-run" not in runner.calls[0][0]


# --------------------------------------------------------------------------
# Status (router contract)
# --------------------------------------------------------------------------


def _hosts(root, *names):
    for n in names:
        fr.add_host(root, n)


class TestFleetStatus:
    def test_pull_without_leader(self, tmp_path, lease):
        _hosts(tmp_path, "worker-a", "worker-b", "worker-c")
        fr.set_override(tmp_path, "worker-c", {"mode": "pin", "account": "1"})
        runner = FakeRunner({
            "worker-a": _ok(_snap(active=2, relogin=["1"])),
            "worker-b": (255, "", "ssh: connect: timed out"),
            "worker-c": _ok(_snap(active=1, state="overridden", settings_hash="fp-other")),
        })
        doc = fr.fleet_status(FakeSwitcher(tmp_path), tmp_path, leader_snapshot=None, runner=runner)
        assert doc["contract"] == fr.CONTRACT
        assert doc["leader"] == {"live": False}
        names = [h["name"] for h in doc["hosts"]]
        assert names == ["local", "worker-a", "worker-b", "worker-c"]
        local, a, b, c = doc["hosts"]
        assert local["local"] and local["state"] == "autonomous" and local["drift"] is False
        assert a["reachable"] and a["source"] == "pull" and a["state"] == "following"
        assert a["active"] == "2" and a["relogin"] == ["1"] and a["drift"] is False
        assert a["lease_age_s"] == 12.0 and a["engine"]["running"]
        assert not b["reachable"] and b["state"] == "unknown" and "timed out" in b["error"]
        assert c["drift"] is True and c["override"]["mode"] == "pin"
        for argv, _ in runner.calls:
            assert argv[6:] == ["cswap", "fleet", "snapshot", "--json"]
        text = json.dumps(doc)
        assert "example.test" not in text and "org-1" not in text
        assert a["accounts"][0]["number"] == "1" and "email" not in a["accounts"][0]
        assert "alias" not in a["accounts"][0] and "usageStatus" in a["accounts"][0]

    def test_leader_snapshot_used(self, tmp_path, lease):
        _hosts(tmp_path, "worker-a", "worker-b", "worker-new")
        leader = {"leader_id": "L1", "hosts": {
            "worker-a": {"reachable": True, "last_ok_at": 1_700_000_000, "error": None,
                         "snapshot": _snap(active=3)},
            "worker-b": {"reachable": False, "last_ok_at": None, "error": "timeout",
                         "snapshot": None},
        }}
        runner = FakeRunner({"worker-new": _ok(_snap(active=2))})
        doc = fr.fleet_status(FakeSwitcher(tmp_path), tmp_path, leader_snapshot=leader, runner=runner)
        assert doc["leader"] == {"live": True, "leader_id": "L1"}
        local, a, b, new = doc["hosts"]
        assert local["state"] == "leading"
        assert a["source"] == "leader" and a["active"] == "3" and a["reachable"]
        assert a["last_ok_at"].endswith("Z")
        assert b["source"] == "leader" and not b["reachable"] and b["error"] == "timeout"
        assert new["source"] == "pull" and new["reachable"]
        assert [c[0][5] for c in runner.calls] == ["worker-new"]

    def test_local_snapshot_failure_is_data(self, tmp_path, lease, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(lease, "build_snapshot", boom)
        doc = fr.fleet_status(FakeSwitcher(tmp_path), tmp_path, leader_snapshot=None)
        assert doc["hosts"][0]["reachable"] is False


class TestLeaderSnapshotLookup:
    def test_reads_fleet_block(self, tmp_path):
        reply = {"result": "running", "engine": {}, "fleet": {
            "role": "leader", "leader": {"leader_id": "L", "hosts": {}}}}
        with patch("claude_swap.engine_control.request", return_value=reply):
            assert fr._leader_snapshot(tmp_path) == {"leader_id": "L", "hosts": {}}

    @pytest.mark.parametrize("reply", [
        {"result": "no-engine"},
        {"result": "running", "engine": {}},
        {"result": "running", "engine": {}, "fleet": {"role": "worker", "leader": None}},
    ])
    def test_no_leader(self, tmp_path, reply):
        with patch("claude_swap.engine_control.request", return_value=reply):
            assert fr._leader_snapshot(tmp_path) is None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _run(capsys, argv, **kw):
    rc = fr.run(argv, **kw)
    out = capsys.readouterr().out
    return rc, out


class TestCli:
    def test_hosts_verbs(self, tmp_path, capsys):
        rc, out = _run(capsys, ["hosts", "add", "worker-a", "--json"], backup_root=tmp_path)
        assert rc == 0 and json.loads(out)["host"]["name"] == "worker-a"
        rc, out = _run(capsys, ["hosts", "list", "--json"], backup_root=tmp_path)
        assert [h["name"] for h in json.loads(out)["hosts"]] == ["worker-a"]
        rc, out = _run(capsys, ["hosts", "list"], backup_root=tmp_path)
        assert "worker-a" in out
        rc, out = _run(capsys, ["hosts", "add", "bad alias"], backup_root=tmp_path)
        assert rc == 1 and out.startswith("error (invalid-host)")
        rc, out = _run(capsys, ["--json", "hosts", "remove", "worker-a"], backup_root=tmp_path)
        assert rc == 0 and json.loads(out)["result"] == "ok"
        rc, out = _run(capsys, ["hosts", "remove", "worker-a", "--json"], backup_root=tmp_path)
        assert rc == 1 and json.loads(out)["error"] == "unknown-host"

    def test_override_verbs(self, tmp_path, capsys):
        fr.add_host(tmp_path, "worker-a")
        assert _run(capsys, ["override", "worker-a", "pin", "2"], backup_root=tmp_path)[0] == 0
        assert fr.load_hosts(tmp_path)[0].override["account"] == "2"
        rc, _ = _run(capsys, ["override", "worker-a", "strategy", "best"], backup_root=tmp_path)
        assert rc == 0 and fr.load_hosts(tmp_path)[0].override["strategy"] == "best"
        assert _run(capsys, ["override", "worker-a", "follow"], backup_root=tmp_path)[0] == 0
        assert fr.load_hosts(tmp_path)[0].override["mode"] == "follow"
        rc, out = _run(capsys, ["override", "worker-z", "follow", "--json"], backup_root=tmp_path)
        assert rc == 1 and json.loads(out)["error"] == "unknown-host"

    @pytest.mark.parametrize("argv", [
        ["override", "worker-a", "pin"],
        ["override", "worker-a", "pin", "zero"],
        ["override", "worker-a", "strategy", "priority-consume-first"],
        ["override", "worker-a", "follow", "2"],
        ["override", "worker-a", "spread"],
        ["hosts"],
        [],
        ["apply", "file.json"],
    ])
    def test_usage_errors_exit_2(self, tmp_path, argv, capsys):
        fr.add_host(tmp_path, "worker-a")
        with pytest.raises(SystemExit) as e:
            fr.run(argv, backup_root=tmp_path)
        assert e.value.code == 2

    def test_status(self, tmp_path, capsys, lease, monkeypatch):
        fr.add_host(tmp_path, "worker-a")
        monkeypatch.setattr(fr, "_leader_snapshot", lambda root: None)
        runner = FakeRunner({"worker-a": (255, "", "refused")})
        rc, out = _run(capsys, ["status", "--json"], backup_root=tmp_path,
                       switcher=FakeSwitcher(tmp_path), runner=runner)
        doc = json.loads(out)
        assert rc == 0 and doc["hosts"][1]["reachable"] is False
        rc, out = _run(capsys, ["status"], backup_root=tmp_path,
                       switcher=FakeSwitcher(tmp_path), runner=runner)
        assert rc == 0 and "worker-a: unreachable" in out and "one-shot pull" in out

    def test_push_exit_codes(self, temp_home, capsys, lease):
        s = PolicyStoreBase()._fleet(temp_home)
        fr.add_host(s.backup_dir, "worker-a")
        fr.add_host(s.backup_dir, "worker-b")
        runner = FakeRunner({"worker-a": _ok({"result": "applied", "changed": False}),
                             "worker-b": (255, "", "x")})
        rc, out = _run(capsys, ["push", "--json"], switcher=s, runner=runner)
        assert rc == 1 and json.loads(out)["error"] == "push-failed"
        rc, out = _run(capsys, ["push", "--host", "worker-a", "--dry-run"],
                       switcher=s, runner=runner)
        assert rc == 0 and "worker-a: no changes" in out
        rc, out = _run(capsys, ["push", "--host", "worker-z", "--json"], switcher=s, runner=runner)
        assert rc == 1 and json.loads(out)["error"] == "unknown-host"

    def test_push_without_hosts(self, tmp_path, capsys, lease):
        rc, out = _run(capsys, ["push", "--json"], backup_root=tmp_path,
                       switcher=FakeSwitcher(tmp_path))
        assert rc == 1 and json.loads(out)["error"] == "no-hosts"

    def test_receive(self, tmp_path, capsys, lease):
        body = {"contract": fr.CONTRACT, "leader": {"id": "L"}}
        rc, out = _run(capsys, ["receive"], switcher=FakeSwitcher(tmp_path),
                       stdin=io.StringIO(json.dumps(body)))
        assert rc == 0 and json.loads(out)["follow"]["state"] == "following"
        assert lease.received == [body]

    def test_receive_invalid(self, tmp_path, capsys, lease):
        lease.receive_error = ValueError("ttl out of range for a@example.test")
        rc, out = _run(capsys, ["receive", "--json"], switcher=FakeSwitcher(tmp_path),
                       stdin=io.StringIO("{}"))
        payload = json.loads(out)
        assert rc == 1 and payload["error"] == "invalid-lease" and "@" not in payload["message"]
        rc, out = _run(capsys, ["receive"], switcher=FakeSwitcher(tmp_path),
                       stdin=io.StringIO("not json"))
        assert rc == 1 and json.loads(out)["error"] == "invalid-input"

    def test_snapshot(self, tmp_path, capsys, lease):
        rc, out = _run(capsys, ["snapshot", "--json"], switcher=FakeSwitcher(tmp_path))
        assert rc == 0 and json.loads(out)["follow"]["state"] == "autonomous"

    def test_apply_via_stdin(self, temp_home, capsys, lease):
        s = PolicyStoreBase()._fleet(temp_home)
        set_setting(s.backup_dir, "autoswitch.threshold", "80")
        doc = fr.export_settings(s)
        set_setting(s.backup_dir, "autoswitch.threshold", "85")
        rc, out = _run(capsys, ["apply", "-", "--dry-run"], switcher=s,
                       stdin=io.StringIO(json.dumps(doc)))
        assert rc == 0 and json.loads(out)["result"] == "dry-run"
        assert load_settings(s.backup_dir).threshold == 85.0
        rc, out = _run(capsys, ["apply"], switcher=s, stdin=io.StringIO(json.dumps(doc)))
        assert rc == 0 and json.loads(out)["result"] == "applied"
        assert load_settings(s.backup_dir).threshold == 80.0

    def test_cli_main_dispatches(self):
        with patch.object(sys, "argv", ["cswap", "fleet", "hosts", "list"]), \
             patch("claude_swap.fleet_remote.main") as m:
            from claude_swap import cli
            cli.main()
        m.assert_called_once_with(["hosts", "list"])


# --------------------------------------------------------------------------
# Settings key
# --------------------------------------------------------------------------


class TestFallbackStrategySetting:
    def test_spec(self):
        spec = SETTING_SPECS["fleet.fallbackStrategy"]
        assert spec.default == "consume-first-reserve-jj"
        assert spec.choices == SELECTABLE_STRATEGIES

    def test_load_default_and_set(self, tmp_path):
        assert load_fleet_settings(tmp_path) == FleetSettings()
        set_setting(tmp_path, "fleet.fallbackStrategy", "best")
        assert load_fleet_settings(tmp_path).fallback_strategy == "best"

    @pytest.mark.parametrize("value", ["priority-consume-first", "nope"])
    def test_rejects_unselectable(self, tmp_path, value):
        with pytest.raises(ConfigError):
            set_setting(tmp_path, "fleet.fallbackStrategy", value)

    def test_corrupt_value_loads_default(self, tmp_path):
        settings_path(tmp_path).write_text(json.dumps({"fleet": {"fallbackStrategy": "nope"}}))
        assert load_fleet_settings(tmp_path) == FleetSettings()
