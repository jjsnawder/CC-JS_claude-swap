"""Tests for fleet_lease: the cswap-fleet/1 lease, worker receive, snapshot, fingerprint.

Every switcher lives in a temp home (file-backed Linux storage); usage fetches
are patched, the engine is absent (no endpoint files) or faked. No real ssh,
store or credentials.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import fleet_lease
from claude_swap.fleet_lease import (
    CONTRACT,
    DEFAULT_TTL_S,
    Lease,
    LeaseError,
    active_lease,
    build_lease,
    build_snapshot,
    follow_state,
    identity_hash,
    lease_path,
    receive_lease,
    relogin_slots,
    settings_fingerprint,
    validate_lease,
)
from tests.test_transfer import _linux_switcher, _live_creds, _seed_account, _usage_row

T0 = 1_800_000_000.0


def _fake_fetch_ok(*_args, **_kwargs):
    from claude_swap import oauth

    return oauth.UsageOutcome({"five_hour": {"pct": 1.0}})


def _two_slot_switcher(home: Path):
    s = _linux_switcher(home)
    _seed_account(s, 1, "alice@example.com", "org-a", creds=_live_creds("alice"))
    _seed_account(s, 2, "bob@example.com", creds=_live_creds("bob"))
    return s


def _lease(
    switcher=None,
    *,
    mode="follow",
    account="2",
    strategy=None,
    ttl_s=DEFAULT_TTL_S,
    seq=1,
    leader_id="leader-a",
    usage=None,
    identities=None,
):
    """A valid lease. ``identities`` (slot → (email, org)) stands in for the
    leader's own store; default: the given switcher's slots."""
    salt = "s" * 32
    if identities is None:
        identities = fleet_lease._slot_identities(switcher) if switcher else {}
    return {
        "contract": CONTRACT,
        "leader": {"id": leader_id, "seq": seq, "ttl_s": ttl_s},
        "target": {"mode": mode, "account": account, "strategy": strategy},
        "identity": {
            "salt": salt,
            "slots": {n: identity_hash(e, o, salt) for n, (e, o) in identities.items()},
        },
        "settings": {"strategy": "consume-first", "threshold": 85.0},
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# identity_hash
# ---------------------------------------------------------------------------


class TestIdentityHash:
    def test_case_insensitive_email_and_salted(self):
        a = identity_hash("Alice@Example.com", "org", "salt1")
        assert a == identity_hash("alice@example.com", "org", "salt1")
        assert a != identity_hash("alice@example.com", "org", "salt2")
        assert a != identity_hash("alice@example.com", "other", "salt1")
        assert identity_hash("x@example.com", None, "s") == identity_hash("x@example.com", "", "s")
        assert len(a) == 64 and "alice" not in a


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_lease_normalizes(self):
        out = validate_lease(_lease(identities={"1": ("a@example.com", "")}, account="02"))
        assert out["target"] == {"mode": "follow", "account": "2", "strategy": None}
        assert out["leader"]["ttl_s"] == float(DEFAULT_TTL_S)
        assert "usage" not in out

    @pytest.mark.parametrize("mutate,message", [
        (lambda l: l.update(contract="cswap-fleet/2"), "contract"),
        (lambda l: l.pop("leader"), "leader must be an object"),
        (lambda l: l["leader"].update(ttl_s=29), "ttl_s"),
        (lambda l: l["leader"].update(ttl_s=901), "ttl_s"),
        (lambda l: l["leader"].update(ttl_s=True), "ttl_s"),
        (lambda l: l["leader"].update(ttl_s=float("nan")), "ttl_s"),
        (lambda l: l["leader"].update(seq=-1), "seq"),
        (lambda l: l["leader"].update(id=""), "leader.id"),
        (lambda l: l["target"].update(mode="spread"), "target.mode"),
        (lambda l: l["target"].update(account=2), "digit string"),
        (lambda l: l["target"].update(account="x1"), "digit string"),
        (lambda l: l["target"].update(account="0"), "1 or more"),
        (lambda l: l["target"].update(account="²"), "digit string"),
        (lambda l: l["target"].update(mode="pin", account=None), "required in pin"),
        (lambda l: l["target"].update(strategy="best"), "null in follow"),
        (lambda l: l["target"].update(mode="strategy", account=None, strategy="nope"),
         "target.strategy"),
        (lambda l: l["target"].update(mode="strategy", strategy="best"), "null in strategy"),
        (lambda l: l.update(identity=[]), "identity must be"),
        (lambda l: l["identity"].update(slots={"a": "h"}), "identity.slots"),
        (lambda l: l.update(settings=None), "settings"),
        (lambda l: l.update(usage=[]), "usage"),
    ])
    def test_malformed_is_rejected(self, mutate, message):
        lease = _lease(identities={"1": ("a@example.com", "")})
        mutate(lease)
        with pytest.raises(LeaseError, match=message):
            validate_lease(lease)

    def test_not_an_object(self):
        with pytest.raises(LeaseError):
            validate_lease([])

    def test_ttl_bounds_inclusive(self):
        for ttl in (30, 900, 45.5):
            validate_lease(_lease(identities={}, ttl_s=ttl))

    def test_strategy_mode(self):
        out = validate_lease(_lease(identities={}, mode="strategy", account=None,
                                    strategy="consume-first-reserve-jj"))
        assert out["target"]["strategy"] == "consume-first-reserve-jj"

    def test_suppressed_strategy_is_rejected(self):
        with pytest.raises(LeaseError):
            validate_lease(_lease(identities={}, mode="strategy", account=None,
                                  strategy="priority-consume-first"))


# ---------------------------------------------------------------------------
# build_lease (dev-box side)
# ---------------------------------------------------------------------------


class TestBuildLease:
    def test_shape_identity_and_usage(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            lease = build_lease(
                s, leader_id="L1", seq=7,
                target={"mode": "follow", "account": "1", "strategy": None},
                settings={"strategy": "best"},
            )
        validate_lease(lease)
        assert lease["contract"] == CONTRACT
        assert lease["leader"] == {"id": "L1", "seq": 7, "ttl_s": DEFAULT_TTL_S}
        salt = lease["identity"]["salt"]
        assert len(salt) == 32
        assert lease["identity"]["slots"] == {
            "1": identity_hash("alice@example.com", "org-a", salt),
            "2": identity_hash("bob@example.com", "", salt),
        }
        # No email outside the usage payload (which the import path needs).
        without_usage = {k: v for k, v in lease.items() if k != "usage"}
        assert "@" not in json.dumps(without_usage)
        assert lease["usage"]["schemaVersion"] == 1
        assert [a["number"] for a in lease["usage"]["accounts"]] == [1, 2]

    def test_salt_is_random_per_lease(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch.object(s, "list_accounts", return_value={"schemaVersion": 1, "accounts": []}):
            a = build_lease(s, leader_id="L", seq=1, target={"mode": "follow", "account": "1"},
                            settings={})
            b = build_lease(s, leader_id="L", seq=2, target={"mode": "follow", "account": "1"},
                            settings={})
        assert a["identity"]["salt"] != b["identity"]["salt"]
        assert a["identity"]["slots"]["1"] != b["identity"]["slots"]["1"]

    def test_bad_target_raises_on_the_leader(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch.object(s, "list_accounts", return_value={"schemaVersion": 1, "accounts": []}):
            with pytest.raises(LeaseError):
                build_lease(s, leader_id="L", seq=1,
                            target={"mode": "pin", "account": None}, settings={})


# ---------------------------------------------------------------------------
# receive_lease + active_lease (worker side)
# ---------------------------------------------------------------------------


class TestReceive:
    def test_receive_writes_lease_and_adopts_usage_with_hold(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        usage = {
            "schemaVersion": 1, "activeAccountNumber": 9,
            "accounts": [_usage_row("bob@example.com", pct=42.0, age_s=400.0)],
        }
        lease = _lease(s, account="2", usage=usage, ttl_s=600)
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            reply = receive_lease(s, lease, now=T0)

        stored = json.loads(lease_path(s.backup_dir).read_text(encoding="utf-8"))
        assert stored["received_at"] == T0
        assert stored["target"] == {"mode": "follow", "account": "2", "strategy": None}
        assert stored["follow_blocked"] is None
        assert "usage" not in stored and "identity" not in stored
        assert "@" not in json.dumps(stored)

        assert reply["contract"] == CONTRACT
        assert reply["lease"] == {
            "accepted": True, "seq": 1, "follow_blocked": None,
            "identity_mismatch": [], "usage_adopted": ["2"], "usage_error": None,
            "changed": True,
        }

        # The hold keeps bob unfetched (400s is past STALE_OK_S) and decision-grade.
        fetched: list[str] = []

        def fake_fetch(num, email, creds, **kwargs):
            fetched.append(num)
            return _fake_fetch_ok()

        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=fake_fetch):
            payload = s.list_accounts(json_output=True)
        assert "2" not in [str(n) for n in fetched]
        bob = next(a for a in payload["accounts"] if a["number"] == 2)
        assert bob["usageStatus"] == "ok"
        assert bob["usage"]["fiveHour"]["pct"] == 42.0

    def test_receive_is_quiet_on_stderr(self, temp_home: Path, capsys):
        s = _two_slot_switcher(temp_home)
        usage = {"schemaVersion": 1, "accounts": [_usage_row("bob@example.com")]}
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            receive_lease(s, _lease(s, usage=usage), now=T0)
        captured = capsys.readouterr()
        assert "@" not in captured.err
        assert "@" not in captured.out

    def test_bad_usage_does_not_block_the_lease(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            reply = receive_lease(s, _lease(s, usage={"schemaVersion": 99}), now=T0)
        assert reply["lease"]["usage_error"] == "invalid-usage-document"
        assert active_lease(s.backup_dir, now=T0 + 1) is not None

    def test_malformed_lease_writes_nothing(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        lease = _lease(s)
        lease["leader"]["ttl_s"] = 5
        with pytest.raises(LeaseError):
            receive_lease(s, lease, now=T0)
        assert not lease_path(s.backup_dir).exists()

    def test_identity_mismatch_blocks_follow(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        # The leader's slot 2 is someone else.
        lease = _lease(identities={
            "1": ("alice@example.com", "org-a"), "2": ("carol@example.com", ""),
        }, account="2")
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            reply = receive_lease(s, lease, now=T0)
        assert reply["lease"]["follow_blocked"] == "identity-mismatch"
        assert reply["lease"]["identity_mismatch"] == ["2"]
        got = active_lease(s.backup_dir, now=T0 + 1)
        assert got is not None and got.follow_blocked == "identity-mismatch"
        assert follow_state(s.backup_dir, None, now=T0 + 1) == {
            "state": "autonomous", "reason": "identity-mismatch", "lease_age_s": 1.0,
            "on_target": None,
        }
        assert got.identity_mismatch == ("2",)

    def test_mismatch_off_target_does_not_block(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        lease = _lease(identities={
            "1": ("someone-else@example.com", ""), "2": ("bob@example.com", ""),
        }, account="2")
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            reply = receive_lease(s, lease, now=T0)
        assert reply["lease"]["follow_blocked"] is None
        assert reply["lease"]["identity_mismatch"] == ["1"]

    def test_target_slot_missing_locally_is_a_mismatch(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        lease = _lease(identities={"3": ("dave@example.com", "")}, account="3")
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            reply = receive_lease(s, lease, now=T0)
        assert reply["lease"]["follow_blocked"] == "identity-mismatch"

    def test_target_absent_from_leader_identity_is_a_mismatch(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        lease = _lease(identities={"1": ("alice@example.com", "org-a")}, account="2")
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            reply = receive_lease(s, lease, now=T0)
        assert reply["lease"]["follow_blocked"] == "identity-mismatch"

    def test_follow_without_account_is_blocked(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            reply = receive_lease(s, _lease(s, account=None), now=T0)
        assert reply["lease"]["follow_blocked"] == "no-target"

    def test_stale_seq_from_same_leader_is_refused(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            receive_lease(s, _lease(s, seq=5, account="2"), now=T0)
            with pytest.raises(LeaseError, match="stale"):
                receive_lease(s, _lease(s, seq=4, account="1"), now=T0 + 1)
            assert active_lease(s.backup_dir, now=T0 + 2).target_account == "2"
            # Same seq (a retransmit) and a new leader (restart) are accepted.
            receive_lease(s, _lease(s, seq=5, account="2"), now=T0 + 2)
            receive_lease(s, _lease(s, seq=0, account="1", leader_id="leader-b"), now=T0 + 3)
        got = active_lease(s.backup_dir, now=T0 + 4)
        assert (got.leader_id, got.seq, got.target_account) == ("leader-b", 0, "1")

    def test_changed_only_when_the_lease_changes_what_the_engine_uses(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)

        def changed(seq, now, account="2", threshold=85.0, **kw):
            lease = _lease(s if "identities" not in kw else None, seq=seq,
                           account=account, ttl_s=600, **kw)
            lease["settings"]["threshold"] = threshold
            return receive_lease(s, lease, now=T0 + now)["lease"]["changed"]

        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            assert changed(1, 0) is True  # no previous lease
            # A heartbeat (new seq, same target/settings/blocked) is not a change.
            assert changed(2, 30) is False
            assert changed(3, 60, account="1") is True  # target
            assert changed(4, 90, account="1", threshold=70.0) is True  # settings
            assert changed(5, 120, account="1", threshold=70.0) is False
            assert changed(6, 150, threshold=70.0) is True
            # Same target and settings, but the leader's #2 is someone else here.
            other = {"1": ("alice@example.com", "org-a"), "2": ("carol@example.com", "")}
            assert changed(7, 180, threshold=70.0, identities=other) is True  # follow_blocked
            assert changed(8, 210, threshold=70.0, identities=other) is False
            # The stored lease expired: the engine is on its fallback, so it is a change.
            assert changed(9, 210 + 601, threshold=70.0, identities=other) is True

class TestActiveLease:
    def _store(self, s, **kw):
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            receive_lease(s, _lease(s, **kw), now=T0)

    def test_expires_on_worker_clock(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s, ttl_s=60, mode="pin", account="1")
        got = active_lease(s.backup_dir, now=T0 + 59.9)
        assert got == Lease(
            leader_id="leader-a", seq=1, ttl_s=60.0, received_at=T0,
            target_mode="pin", target_account="1", target_strategy=None,
            settings={"strategy": "consume-first", "threshold": 85.0},
            follow_blocked=None,
        )
        assert active_lease(s.backup_dir, now=T0 + 60) is None

    def test_clock_stepped_back_distrusts_lease(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s)
        assert active_lease(s.backup_dir, now=T0 - 3) is not None  # within slack
        assert active_lease(s.backup_dir, now=T0 - 3600) is None

    def test_missing_or_corrupt_is_none(self, temp_home: Path):
        root = temp_home / "root"
        assert active_lease(root, now=T0) is None
        lease_path(root).parent.mkdir(parents=True)
        lease_path(root).write_text("{not json", encoding="utf-8")
        assert active_lease(root, now=T0) is None
        lease_path(root).write_text(json.dumps({"contract": CONTRACT}), encoding="utf-8")
        assert active_lease(root, now=T0) is None

    def test_hand_edited_ttl_out_of_bounds_is_none(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s)
        path = lease_path(s.backup_dir)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["leader"]["ttl_s"] = 10**9
        path.write_text(json.dumps(record), encoding="utf-8")
        assert active_lease(s.backup_dir, now=T0 + 1) is None

    def test_default_now_is_wall_clock(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            receive_lease(s, _lease(s))
        assert active_lease(s.backup_dir) is not None


# ---------------------------------------------------------------------------
# follow_state
# ---------------------------------------------------------------------------


class TestFollowState:
    RUNNING = {"result": "running", "engine": {"mode": "live"}}

    def _store(self, s, **kw):
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            receive_lease(s, _lease(s, **kw), now=T0)

    def test_no_lease(self, temp_home: Path):
        assert follow_state(temp_home / "root", self.RUNNING, now=T0) == {
            "state": "autonomous", "reason": "no-lease", "lease_age_s": None,
            "on_target": None,
        }

    def test_following(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s)
        assert follow_state(s.backup_dir, self.RUNNING, now=T0 + 12.34) == {
            "state": "following", "reason": None, "lease_age_s": 12.3,
            "on_target": None,
        }

    def test_overridden_pin_and_strategy(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s, mode="pin", account="1")
        assert follow_state(s.backup_dir, None, now=T0)["state"] == "overridden"
        assert follow_state(s.backup_dir, None, now=T0)["reason"] == "pin 1"
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            receive_lease(s, _lease(s, mode="strategy", account=None, strategy="best", seq=2),
                          now=T0)
        assert follow_state(s.backup_dir, None, now=T0)["reason"] == "strategy best"

    def test_expired(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s, ttl_s=30)
        assert follow_state(s.backup_dir, self.RUNNING, now=T0 + 31) == {
            "state": "autonomous", "reason": "lease-expired", "lease_age_s": 31.0,
            "on_target": None,
        }

    def test_no_engine_and_dry_run(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s)
        assert follow_state(s.backup_dir, {"result": "no-engine"}, now=T0)["reason"] == "no-engine"
        dry = {"result": "running", "engine": {"mode": "dry-run"}}
        assert follow_state(s.backup_dir, dry, now=T0)["reason"] == "engine-dry-run"
        # A transport error is not judged: the lease decides.
        err = {"result": "error", "error": "transport"}
        assert follow_state(s.backup_dir, err, now=T0)["state"] == "following"

    def test_on_target(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s, account="2")
        on = follow_state(s.backup_dir, self.RUNNING, now=T0, active="2")
        assert on["state"] == "following" and on["on_target"] is True
        off = follow_state(s.backup_dir, self.RUNNING, now=T0, active=1)
        assert off["state"] == "following" and off["on_target"] is False
        assert follow_state(s.backup_dir, self.RUNNING, now=T0)["on_target"] is None
        # An expired lease has no target to be on.
        assert follow_state(s.backup_dir, self.RUNNING, now=T0 + 10**4,
                            active="2")["on_target"] is False

    def test_on_target_strategy_lease_is_false(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        self._store(s, mode="strategy", account=None, strategy="best")
        assert follow_state(s.backup_dir, None, now=T0, active="2")["on_target"] is False


class TestForgetLease:
    def test_forget_removes_the_lease(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            receive_lease(s, _lease(s), now=T0)
        assert fleet_lease.forget_lease(s.backup_dir) is True
        assert not lease_path(s.backup_dir).exists()
        assert active_lease(s.backup_dir, now=T0 + 1) is None
        assert follow_state(s.backup_dir, None, now=T0)["reason"] == "no-lease"
        assert fleet_lease.forget_lease(s.backup_dir) is False

    def test_forget_with_no_fleet_dir_creates_nothing(self, temp_home: Path):
        root = temp_home / "root"
        root.mkdir()
        assert fleet_lease.forget_lease(root) is False
        assert not (root / "fleet").exists()


class TestListPayload:
    def test_store_only(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        payload = {"schemaVersion": 1, "accounts": []}
        with patch.object(s, "list_accounts", return_value=payload) as la:
            assert fleet_lease.list_payload(s) is payload
        la.assert_called_once_with(json_output=True, fetch=set())

    def test_build_lease_uses_a_given_usage(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        usage = {"schemaVersion": 1, "accounts": []}
        with patch.object(s, "list_accounts", side_effect=AssertionError("listed")):
            lease = build_lease(s, leader_id="L", seq=1,
                                target={"mode": "follow", "account": "1"},
                                settings={}, usage=usage)
        assert lease["usage"] is usage

    def test_constants_have_one_home(self):
        from claude_swap import fleet_leader

        assert fleet_leader.FLEET_DIRNAME is fleet_lease.FLEET_DIRNAME
        assert fleet_leader.LEASE_FILENAME is fleet_lease.LEASE_FILENAME
        assert fleet_leader.HOSTS_FILENAME is fleet_lease.HOSTS_FILENAME


# ---------------------------------------------------------------------------
# relogin + snapshot
# ---------------------------------------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


class TestRelogin:
    def test_all_three_sources(self, temp_home: Path):
        root = temp_home / "root"
        root.mkdir()
        (root / "autoswitch_state.json").write_text(json.dumps({
            "quarantine": {"4": {"reason": "invalid_grant"}},
        }), encoding="utf-8")
        payload = {"accounts": [
            {"number": 1, "usageStatus": "ok"},
            {"number": 2, "usageStatus": "relogin_required"},
            {"number": 3, "usageStatus": "ok", "loginExpiresAt": _iso(T0 + 6 * 86400)},
            {"number": 5, "usageStatus": "ok", "loginExpiresAt": _iso(T0 + 8 * 86400)},
            {"number": 6, "usageStatus": "ok", "loginExpiresAt": _iso(T0 - 86400)},
            {"number": 7, "usageStatus": "ok", "loginExpiresAt": "garbage"},
        ]}
        assert relogin_slots(payload, root, now=T0) == ["2", "3", "4", "6"]

    def test_corrupt_state_file_is_ignored(self, temp_home: Path):
        root = temp_home / "root"
        root.mkdir()
        (root / "autoswitch_state.json").write_text("[1,2]", encoding="utf-8")
        assert relogin_slots({"accounts": []}, root, now=T0) == []


class TestSnapshot:
    def test_shape_without_engine(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        (Path(s.backup_dir) / "autoswitch_state.json").write_text(json.dumps({
            "quarantine": {"1": {"reason": "invalid_grant"}},
        }), encoding="utf-8")
        with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=_fake_fetch_ok):
            snap = build_snapshot(s, s.backup_dir, now=T0)
        assert set(snap) == {
            "contract", "host_time", "accounts", "engine", "follow", "relogin",
            "relogin_detail", "settings_hash",
        }
        assert snap["contract"] == CONTRACT
        assert snap["host_time"] == T0
        assert snap["accounts"]["schemaVersion"] == 1
        assert [a["number"] for a in snap["accounts"]["accounts"]] == [1, 2]
        assert snap["engine"]["result"] == "no-engine"
        assert snap["follow"] == {"state": "autonomous", "reason": "no-engine", "lease_age_s": None,
                                  "on_target": None}
        assert snap["relogin"] == ["1"]
        assert snap["relogin_detail"] == [
            {"slot": "1", "reason": "quarantined", "expires_at": None}]
        assert snap["settings_hash"] == settings_fingerprint(s)
        json.dumps(snap)  # serializable

    def test_unreadable_state_file_sends_null_detail(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        (Path(s.backup_dir) / "autoswitch_state.json").write_text(
            '{"quarantine": {"1": ', encoding="utf-8")  # mid-write
        with patch.object(s, "list_accounts", return_value={"schemaVersion": 1, "accounts": []}):
            snap = build_snapshot(s, s.backup_dir, now=T0)
        assert snap["relogin_detail"] is None  # unknown, not "nothing to log in"
        assert snap["relogin"] == []
        json.dumps(snap)

    def test_engine_reply_is_carried(self, temp_home: Path, monkeypatch):
        s = _two_slot_switcher(temp_home)
        reply = {"contract": "cswap-engine/0.1", "op": "status", "result": "running",
                 "engine": {"mode": "live", "active": "2"}}
        monkeypatch.setattr("claude_swap.engine_control.request", lambda *a, **k: reply)
        with patch.object(s, "list_accounts", return_value={"schemaVersion": 1, "accounts": []}):
            snap = build_snapshot(s, s.backup_dir, now=T0)
        assert snap["engine"] == reply
        assert snap["follow"]["reason"] == "no-lease"

    def test_list_failure_degrades(self, temp_home: Path):
        s = _two_slot_switcher(temp_home)
        with patch.object(s, "list_accounts", side_effect=RuntimeError("boom a@example.com")):
            snap = build_snapshot(s, s.backup_dir, now=T0)
        assert snap["accounts"]["accounts"] == []
        assert snap["accounts"]["error"] == "list-failed"
        assert "@" not in json.dumps(snap["accounts"])


# ---------------------------------------------------------------------------
# settings_fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def _pair(self, temp_home: Path):
        """Two independent stores with the same accounts (two 'boxes')."""
        from claude_swap import paths

        boxes = []
        for name in ("box-a", "box-b"):
            home = temp_home / name
            (home / ".claude").mkdir(parents=True)
            with patch("pathlib.Path.home", return_value=home), \
                 patch.dict("os.environ", {"HOME": str(home), "USERPROFILE": str(home)}):
                s = _two_slot_switcher(home)
            boxes.append(s)
        assert boxes[0].backup_dir != boxes[1].backup_dir
        return boxes

    def test_equal_for_identical_settings(self, temp_home: Path):
        from claude_swap.settings import set_setting

        a, b = self._pair(temp_home)
        assert settings_fingerprint(a) == settings_fingerprint(b)
        for s in (a, b):
            set_setting(s.backup_dir, "autoswitch.threshold", "85")
            s.set_account_threshold("1", 70)
        assert settings_fingerprint(a) == settings_fingerprint(b)
        # Box-local sections and explicit defaults do not count.
        set_setting(a.backup_dir, "ui.theme", "dark")
        set_setting(a.backup_dir, "updates.check", "false")
        set_setting(a.backup_dir, "autoswitch.strategy", "best")  # the default
        assert settings_fingerprint(a) == settings_fingerprint(b)

    @pytest.mark.parametrize("change", [
        lambda s: __import__("claude_swap.settings", fromlist=["x"]).set_setting(
            s.backup_dir, "autoswitch.strategy", "consume-first"),
        lambda s: __import__("claude_swap.settings", fromlist=["x"]).set_setting(
            s.backup_dir, "autoswitch.reserveAccounts", "2"),
        lambda s: s.set_account_threshold("2", 60),
        lambda s: s.set_account_priority("1", 3),
        lambda s: s.set_account_disabled("2", True),
    ])
    def test_unequal_on_any_policy_change(self, temp_home: Path, change):
        a, b = self._pair(temp_home)
        change(a)
        assert settings_fingerprint(a) != settings_fingerprint(b)

    def test_deterministic_and_hex(self, temp_home: Path):
        a, _ = self._pair(temp_home)
        fp = settings_fingerprint(a)
        assert fp == settings_fingerprint(a)
        assert len(fp) == 64 and int(fp, 16) >= 0
        doc = fleet_lease.fingerprint_document(a)
        assert "@" not in json.dumps(doc)
