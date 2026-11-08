"""Fork (fleet): the worker follow hook in AutoSwitchEngine.

The lease itself (fleet_lease.py) is stubbed by its Internal API names:
``active_lease`` is monkeypatched, and ``<backup_root>/fleet/lease.json``
merely exists (the "has ever received a lease" marker the hook stats).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import fleet_lease, fleet_leader
from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome, WarmupEvent
from claude_swap.fleet_lease import Lease

import tests.test_autoswitch as ta
from tests.test_autoswitch import (  # noqa: F401
    EngineHarness,
    _R_LATER,
    _R_LATEST,
    _R_SOON,
    _usage,
    _usage7,
)


def _lease(mode="follow", account=None, strategy=None, settings=None, blocked=None,
           mismatch=()):
    return Lease(
        leader_id="leader-test",
        seq=1,
        ttl_s=180.0,
        received_at=0.0,
        target_mode=mode,
        target_account=account,
        target_strategy=strategy,
        settings=settings or {},
        follow_blocked=blocked,
        identity_mismatch=tuple(mismatch),
    )


class LeaseBox:
    """What ``active_lease`` returns; ``None`` = expired / absent."""

    def __init__(self, monkeypatch, root: Path):
        self.lease = None
        self.calls = 0
        self.root = root
        monkeypatch.setattr(fleet_lease, "active_lease", self._active)

    def _active(self, backup_root, *, now=None):
        self.calls += 1
        assert Path(backup_root) == self.root
        return self.lease

    def mark_history(self) -> None:
        d = self.root / "fleet"
        d.mkdir(parents=True, exist_ok=True)
        (d / "lease.json").write_text("{}", encoding="utf-8")


def _harness(temp_home, **kw) -> EngineHarness:
    h = EngineHarness(temp_home, **kw)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


@pytest.fixture
def h(temp_home):
    return _harness(temp_home)


@pytest.fixture
def box(h, monkeypatch):
    b = LeaseBox(monkeypatch, h.switcher.backup_dir)
    b.mark_history()
    return b


def _reasons(h):
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _switches(h):
    return [e for e in h.events if isinstance(e, SwitchEvent)]


_HEALTHY = {"1": _usage(30), "2": _usage(20), "3": _usage(40)}
# consume-first: #2 resets soonest (its nudge target), #3 latest.
_CF = {
    "1": _usage7(20, 20, _R_LATER),
    "2": _usage7(10, 10, _R_SOON),
    "3": _usage7(10, 10, _R_LATEST),
}


class TestFollow:
    def test_follows_the_target_through_the_manual_path(self, h, box):
        box.lease = _lease(account="3")
        freshened = []
        real = h.engine._freshen_target

        def spy(num, email):
            freshened.append(num)
            return real(num, email)

        with patch.object(h.engine, "_freshen_target", side_effect=spy):
            assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert freshened == ["3"]  # the strategy's pick (#2) was never tried
        sw = _switches(h)[-1]
        assert sw.trigger == "fleet" and "fleet leader" in sw.note
        assert h.engine.pinned is None  # a follow is not a held `m` pick
        assert h.state()["leftTrigger"] == "manual"  # same record as `m`

    def test_follow_logs_its_source_and_drops_a_hold(self, h, box, caplog):
        """fork: pin-control -- the switch-source line names the lease."""
        import logging

        caplog.set_level(logging.INFO, logger="claude-swap")
        h.engine._pinned = "1"
        box.lease = _lease(account="3")
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        lines = [r.getMessage() for r in caplog.records if r.name == "claude-swap"]
        assert (
            "auto-switch: engine moved #1 -> #3 (trigger fleet-follow, source fleet-lease)"
            in lines
        )
        assert "auto-switch: hold on #1 released (follow)" in lines
        assert h.engine.pinned is None

    def test_follow_ignores_cooldown(self, h, box):
        h.engine._mutate_state(lambda st: st.__setitem__("lastSwitchAt", h.clock.now))
        box.lease = _lease(account="2")
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert "cooldown" not in _reasons(h)

    def test_on_target_is_a_no_op(self, h, box):
        box.lease = _lease(account="1")
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.NO_ACTION
        assert _switches(h) == []
        assert h.active_number() == 1

    def test_strategy_never_undoes_the_follow_below_the_line(self, temp_home, monkeypatch):
        """consume-first would nudge to #2; the lease holds the box on #3."""
        h = _harness(temp_home, strategy="consume-first", reserve_accounts="none")
        box = LeaseBox(monkeypatch, h.switcher.backup_dir)
        box.mark_history()
        box.lease = _lease(account="3")
        assert h.tick_with_usage(_CF) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        h.clock.advance(h.settings.cooldown_seconds + 1)
        h.events.clear()
        assert h.tick_with_usage(_CF) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["fleet-follow"]
        assert h.active_number() == 3

    def test_over_the_line_on_target_holds(self, h, box):
        """Every box shares the account: the leader moves them all."""
        box.lease = _lease(account="1")
        usage = {"1": _usage(95), "2": _usage(10), "3": _usage(10)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["fleet-follow"]
        assert h.active_number() == 1

    def test_at_limit_on_target_escapes_and_does_not_bounce_back(self, h, box):
        box.lease = _lease(account="1")
        usage = {"1": _usage(100), "2": _usage(10), "3": _usage(30)}
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _switches(h)[-1].trigger == "at-limit"
        assert h.active_number() == 2
        assert h.engine._fleet.refusal_public()["reason"] == "escaped"
        h.events.clear()
        h.clock.advance(60)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _switches(h) == []  # not back onto #1 while it is at its limit

    def test_refused_target_is_reported_and_not_retried_every_tick(self, h, box):
        box.lease = _lease(account="3")
        usage = {"1": _usage(30), "2": _usage(20), "3": _usage(95)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["target-over-threshold"]
        refusal = h.engine._fleet.refusal_public()
        assert refusal["target"] == "3" and refusal["reason"] == "target-over-threshold"
        h.events.clear()
        h.clock.advance(60)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert "target-over-threshold" not in _reasons(h)
        # After the retry window the pick runs again, and lands once it can.
        h.clock.advance(fleet_leader.FOLLOW_RETRY_S)
        h.events.clear()
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert h.engine._fleet.refusal_public() is None

    def test_a_new_target_clears_the_refusal(self, h, box):
        box.lease = _lease(account="3")
        h.tick_with_usage({"1": _usage(30), "2": _usage(20), "3": _usage(95)})
        assert h.engine._fleet.refusal is not None
        box.lease = _lease(account="2")
        h.clock.advance(60)
        assert h.tick_with_usage({"1": _usage(30), "2": _usage(20), "3": _usage(95)}) \
            is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_quarantined_target_is_refused_like_m(self, h, box):
        h.engine._quarantine("3", "c@example.com", "invalid_grant")
        h.events.clear()
        box.lease = _lease(account="3")
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["target-quarantined"]
        assert h.active_number() == 1

    def test_users_own_pick_wins_its_tick_then_the_lease_resumes(self, h, box):
        """Overrides belong to the leader (Hosts menu); a local `m` is not
        an escape, so the next tick follows again."""
        box.lease = _lease(account="1")
        h.engine.request_switch(target="2")
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert _switches(h)[-1].trigger == "manual"
        assert h.active_number() == 2
        h.clock.advance(60)
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert _switches(h)[-1].trigger == "fleet"
        assert h.active_number() == 1

    def test_target_busy_retries_next_tick(self, h, box):
        """A hello in flight on the target clears in seconds: the follow is
        retried on the very next tick, not parked for FOLLOW_RETRY_S."""
        box.lease = _lease(account="3")
        with h.engine._warm_lock:
            h.engine._warm_inflight.add("3")
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["target-busy"]
        assert h.engine._fleet.refusal_public()["reason"] == "target-busy"
        with h.engine._warm_lock:
            h.engine._warm_inflight.discard("3")
        h.events.clear()
        h.clock.advance(60)
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert h.engine._fleet.refusal_public() is None

    def test_usage_unknown_retries_next_tick(self, h, box):
        box.lease = _lease(account="3")
        usage = {"1": _HEALTHY["1"], "2": _HEALTHY["2"], "3": None}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert "target-usage-unknown" in _reasons(h)
        h.events.clear()
        h.clock.advance(60)
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert h.active_number() == 3

    @pytest.mark.parametrize("reason,outcome,held", [
        ("target-busy", TickOutcome.NO_ACTION, False),
        ("target-usage-unknown", TickOutcome.NO_ACTION, False),
        ("no-comparison", TickOutcome.BLOCKED, False),
        ("something", TickOutcome.BLOCKED, False),
        ("target-over-threshold", TickOutcome.NO_ACTION, True),
    ])
    def test_refusal_retry_window(self, h, reason, outcome, held):
        fleet = h.engine._fleet
        fleet.following, fleet._attempt = True, "3"
        h.engine._tick_events = [NoSwitchEvent(reason=reason, detail="")]
        fleet.end_tick(outcome)
        now = h.clock()
        expect = now + fleet_leader.FOLLOW_RETRY_S if held else now
        assert fleet.refusal["retry_at"] == expect

    def test_blocked_lease_does_not_follow(self, h, box):
        box.lease = _lease(account="3", blocked="identity-mismatch")
        h.tick_with_usage(_HEALTHY)
        assert [e.trigger for e in _switches(h) if e.trigger == "fleet"] == []
        assert h.engine._fleet.tick.mode == "fallback"


class TestPinAndStrategy:
    def test_pin_targets_its_account(self, h, box):
        box.lease = _lease(mode="pin", account="2")
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.events.clear()
        h.clock.advance(60)
        assert h.tick_with_usage(_HEALTHY) is TickOutcome.NO_ACTION
        assert h.active_number() == 2

    def test_strategy_override_ranks_with_that_strategy(self, h, box):
        # `best`: below the line it never moves.
        box.lease = _lease(mode="strategy", strategy="best")
        assert h.tick_with_usage(_CF) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        h.events.clear()
        box.lease = _lease(mode="strategy", strategy="consume-first",
                           settings={"reserveAccounts": "none"})
        assert h.tick_with_usage(_CF) is TickOutcome.SWITCHED
        assert _switches(h)[-1].trigger == "consume-first"
        assert h.active_number() == 2
        assert h.engine.settings.strategy == "best"  # never persisted / swapped

    def test_lease_threshold_applies_for_the_tick_only(self, h, box):
        usage = {"1": _usage(85), "2": _usage(10), "3": _usage(10)}
        box.lease = _lease(mode="strategy", strategy="best", settings={"threshold": 80.0})
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED  # 85 >= 80
        assert h.engine.settings.threshold == 90.0
        assert json.loads((h.switcher.backup_dir / "fleet" / "lease.json").read_text()) == {}

    def test_lease_per_account_threshold(self, h, box):
        usage = {"1": _usage(75), "2": _usage(10), "3": _usage(10)}
        box.lease = _lease(mode="strategy", strategy="best",
                           settings={"accounts": {"1": {"threshold": 70.0}}})
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED

    def test_bad_lease_settings_keep_local_values(self):
        from claude_swap.settings import AutoSwitchSettings

        base = AutoSwitchSettings()
        s, thresholds, disabled = fleet_leader.apply_lease_settings(
            base, {"strategy": "nope", "threshold": float("nan"), "model": 3,
                   "accounts": {"x": {"threshold": 50}, "2": {"disabled": True}}},
        )
        assert s == base and thresholds == {} and disabled == frozenset({"2"})

    def test_identity_mismatched_slots_skip_leader_policy(self):
        from claude_swap.settings import AutoSwitchSettings

        doc = {"accounts": {"1": {"threshold": 50.0, "disabled": True},
                            "2": {"threshold": 60.0, "disabled": True}}}
        _, thresholds, disabled = fleet_leader.apply_lease_settings(
            AutoSwitchSettings(), doc, mismatched=("1",)
        )
        assert thresholds == {"2": 60.0} and disabled == frozenset({"2"})

    def test_identity_mismatched_slots_are_never_reserved(self):
        from dataclasses import replace

        from claude_swap.settings import AutoSwitchSettings

        base = replace(AutoSwitchSettings(), reserve_accounts="3")
        s, _, _ = fleet_leader.apply_lease_settings(
            base, {"reserveAccounts": "1,2"}, mismatched=("1",))
        assert s.reserve_accounts == "2"
        # Nothing valid left: the box keeps its own reserve, not the leader's #1.
        s, _, _ = fleet_leader.apply_lease_settings(
            base, {"reserveAccounts": "1"}, mismatched=("1",))
        assert s.reserve_accounts == "3"
        s, _, _ = fleet_leader.apply_lease_settings(
            base, {"reserveAccounts": "none"}, mismatched=("1",))
        assert s.reserve_accounts == "none"

    def test_worker_tick_reserve_skips_mismatched_slot(self, h, box):
        box.lease = _lease(mode="strategy", strategy="best", mismatch=("1",),
                           settings={"reserveAccounts": "1,3"})
        h.tick_with_usage(_HEALTHY)
        assert h.engine._fleet.tick.settings.reserve_accounts == "3"

    def test_worker_tick_drops_mismatched_slots(self, h, box):
        box.lease = _lease(mode="strategy", strategy="best", mismatch=("1",),
                           settings={"accounts": {"1": {"threshold": 10.0, "disabled": True},
                                                  "3": {"disabled": True}}})
        usage = {"1": _usage(75), "2": _usage(10), "3": _usage(10)}
        # The leader's #1 line (10%) is another account here: #1 stays put.
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        tick = h.engine._fleet.tick
        assert tick.thresholds == {} and tick.disabled == frozenset({"3"})


class TestFallback:
    def test_expired_lease_uses_the_fallback_strategy_then_rejoins(self, h, box):
        """Default fallback consume-first-reserve-jj leaves reserve #1 (the
        active one) for a healthy non-reserve; `best` would stay put."""
        box.lease = None
        assert h.tick_with_usage(_CF) is TickOutcome.SWITCHED
        assert h.engine._fleet.tick.settings.strategy == "consume-first-reserve-jj"
        assert h.active_number() != 1
        h.events.clear()
        h.clock.advance(30)  # inside the cooldown: a follow is exempt
        box.lease = _lease(account="1")
        assert h.tick_with_usage(_CF) is TickOutcome.SWITCHED
        assert _switches(h)[-1].trigger == "fleet"
        assert h.active_number() == 1

    def test_fallback_strategy_setting(self, h, box):
        (h.switcher.backup_dir / "settings.json").write_text(json.dumps(
            {"schemaVersion": 1, "fleet": {"fallbackStrategy": "consume-first"}}
        ))
        box.lease = None
        h.tick_with_usage(_HEALTHY)
        assert h.engine._fleet.tick.settings.strategy == "consume-first"


class TestSleepBackstop:
    def _blocked_long(self, h):
        h.engine._sleep_until_ts = h.clock() + 3600
        h.engine._warm_deadline_ts = None

    def test_valid_lease_caps_sleep_at_interval(self, h, box):
        box.lease = _lease(account="1")
        h.tick_with_usage(_HEALTHY)
        self._blocked_long(h)
        assert h.engine._next_delay(TickOutcome.BLOCKED) <= h.engine.settings.interval_seconds

    def test_fallback_and_no_fleet_do_not_cap(self, h, box):
        box.lease = None
        h.tick_with_usage(_HEALTHY)
        self._blocked_long(h)
        assert h.engine._next_delay(TickOutcome.BLOCKED) > h.engine.settings.interval_seconds
        h.engine._fleet.tick = None
        assert h.engine._next_delay(TickOutcome.BLOCKED) > h.engine.settings.interval_seconds

    def test_blocked_lease_does_not_cap(self, h, box):
        box.lease = _lease(account="3", blocked="identity-mismatch")
        h.tick_with_usage(_HEALTHY)
        self._blocked_long(h)
        assert h.engine._next_delay(TickOutcome.BLOCKED) > h.engine.settings.interval_seconds


class TestNoFleetBox:
    def test_no_fleet_files_is_a_pass_through(self, temp_home, monkeypatch):
        """Same events and the same clock reads as an engine whose fleet
        hooks are all identity stubs (i.e. upstream's engine)."""

        class NullFleet:
            following = False
            in_loop = False
            leader = None

            def begin_tick(self, s): return s
            def policies(self, p): return p
            def switchable(self, n): return n
            def follow_target(self, c): return None
            def hold_detail(self, c, *, over): return None
            def warmup_suppressed(self): return False
            def event_trigger(self, t): return t
            def after_switch(self, f): return ""
            def end_tick(self, o): pass
            def cap_delay(self, d, i): return d
            def ensure_leader(self): pass
            def stop_leader(self): pass

        def run(h, fleet=None):
            if fleet is not None:
                h.engine._fleet = fleet
            reads = []
            real = h.clock

            def counting():
                reads.append(1)
                return real()

            h.engine.clock = counting
            outcomes = [h.tick_with_usage(_CF), h.tick_with_usage(
                {"1": _usage(95), "2": _usage(10), "3": _usage(20)})]
            return outcomes, [(e.kind, getattr(e, "reason", None)) for e in h.events], len(reads)

        for sub in ("a", "b"):
            (temp_home / sub / ".claude").mkdir(parents=True)
        h1 = _harness(temp_home / "a")
        box = LeaseBox(monkeypatch, h1.switcher.backup_dir)  # NOT marked
        base = h1.engine.settings
        assert h1.engine._fleet.begin_tick(base) is base
        real = run(h1)
        stub = run(_harness(temp_home / "b"), NullFleet())
        assert real == stub
        assert real[0][0] is TickOutcome.NO_ACTION and real[2] > 0
        assert box.calls == 0
        assert h1.engine._fleet.tick is None
        assert not (h1.switcher.backup_dir / "fleet").exists()

    def test_no_fleet_engine_never_starts_a_leader(self, h, monkeypatch):
        from claude_swap import fleet_remote

        monkeypatch.setattr(
            fleet_remote, "load_hosts",
            lambda root: pytest.fail("load_hosts read on a box with no fleet files"),
        )
        h.engine._fleet.in_loop = True
        h.engine._fleet.ensure_leader()
        assert h.engine._fleet.leader is None


class TestWarmupWhileFollowing:
    """Following: the leader's engine warms the shared accounts, so a
    follower spawns no hellos; fallback warms per its own settings."""

    def _dry_warm(self, temp_home, monkeypatch):
        t = ta.TestWarmup()
        h = t._warm_harness(temp_home, monkeypatch, warmup_stagger=False)
        h.engine = h._make_engine(dry_run=True)
        h.engine.ping_runner = h.runner
        box = LeaseBox(monkeypatch, h.switcher.backup_dir)
        box.mark_history()
        return t, h, box

    def test_following_box_plans_no_hellos(self, temp_home, monkeypatch):
        t, h, box = self._dry_warm(temp_home, monkeypatch)
        box.lease = _lease(account="1")
        t._tick(h, t._cold())
        assert [e for e in h.events if isinstance(e, WarmupEvent)] == []
        assert h.runner.calls == []

    def test_fallback_box_warms_per_its_settings(self, temp_home, monkeypatch):
        t, h, box = self._dry_warm(temp_home, monkeypatch)
        box.lease = None
        t._tick(h, t._cold())
        assert [e.action for e in h.events if isinstance(e, WarmupEvent)]
        assert h.runner.calls == []  # dry-run: previews only
