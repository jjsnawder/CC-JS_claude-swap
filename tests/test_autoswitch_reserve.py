"""consume-first-reserve-jj (fork-only): reserve accounts are consumed LAST.

Spec: docs/plans/2026-11-01-consume-first-reserve.md, "Behaviour contract".
Reuses the engine harness and usage builders from test_autoswitch.py.
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    RESERVE_STRATEGY,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    _reserve_departure_note,
    _reserve_tier,
    is_consume_first,
    reserve_accounts,
    strategy_cycle,
)
from claude_swap.settings import AutoSwitchSettings
from tests.test_autoswitch import (
    _R_LATER,
    _R_LATEST,
    _R_SOON,
    EngineHarness,
    _iso_at,
    _usage7,
    _usage75,
)


def _reserve_harness(temp_home: Path, reserve: str | None = "2", **kw) -> EngineHarness:
    h = EngineHarness(
        temp_home, strategy=RESERVE_STRATEGY, reserve_accounts=reserve, **kw
    )
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _on(h: EngineHarness, num: int, email: str) -> None:
    """Make ``num`` the active account (live login + sequence)."""
    h.make_live(email, num)
    data = h.switcher._get_sequence_data()
    data["activeAccountNumber"] = num
    h.switcher._write_json(h.switcher.sequence_file, data)


def _switch(h: EngineHarness) -> SwitchEvent:
    return next(e for e in h.events if isinstance(e, SwitchEvent))


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


# -- helpers -------------------------------------------------------------------


class TestHelpers:
    def test_is_consume_first_covers_both_variants(self):
        assert is_consume_first("consume-first")
        assert is_consume_first(RESERVE_STRATEGY)
        assert not is_consume_first("best")
        assert not is_consume_first(None)

    def test_reserve_set_only_under_the_reserve_strategy(self):
        assert reserve_accounts(
            AutoSwitchSettings(strategy=RESERVE_STRATEGY, reserve_accounts="1,4")
        ) == frozenset({"1", "4"})
        assert reserve_accounts(
            AutoSwitchSettings(strategy="consume-first", reserve_accounts="1")
        ) == frozenset()
        # the default reserve is account 1
        assert reserve_accounts(AutoSwitchSettings(strategy=RESERVE_STRATEGY)) == (
            frozenset({"1"})
        )
        for empty in ("none", "", None):
            assert reserve_accounts(
                AutoSwitchSettings(strategy=RESERVE_STRATEGY, reserve_accounts=empty)
            ) == frozenset()
        # the default never reaches the other strategies
        for strategy in ("best", "consume-first"):
            assert reserve_accounts(AutoSwitchSettings(strategy=strategy)) == (
                frozenset()
            )

    def test_strategy_cycle_hides_reserve_variant_only_for_none(self):
        choices = ("best", "consume-first", RESERVE_STRATEGY)
        assert strategy_cycle(choices, AutoSwitchSettings()) == choices  # "1"
        assert strategy_cycle(
            choices, AutoSwitchSettings(reserve_accounts="1,4")
        ) == choices
        for empty in ("none", "", None):
            assert strategy_cycle(
                choices, AutoSwitchSettings(reserve_accounts=empty)
            ) == ("best", "consume-first")

    def test_tier_and_note(self):
        reserves = frozenset({"2", "4"})
        headroom = {"2": 50.0, "3": 5.0, "4": 5.0}
        assert _reserve_tier("2", reserves, headroom, 90.0) == 0
        assert _reserve_tier("3", reserves, headroom, 90.0) == 1
        assert _reserve_tier("4", reserves, headroom, 90.0) == 2
        assert _reserve_departure_note(2, reserves) == "left reserve account #2"
        assert _reserve_departure_note("3", reserves) == ""
        assert _reserve_departure_note(None, reserves) == ""

    def test_tier_reads_the_per_account_line_verdict(self):
        """`over` (the engine's per-window verdict) replaces the single-line
        test: 7 pts of raw headroom is over a 90 line but under a 97 weekly
        one."""
        reserves = frozenset({"2"})
        headroom = {"2": 7.0}
        assert _reserve_tier("2", reserves, headroom, 90.0) == 2
        assert _reserve_tier("2", reserves, headroom, 90.0, {"2": False}) == 0
        assert _reserve_tier("2", reserves, headroom, 90.0, {"2": True}) == 2

    def test_reserve_gate_reads_each_accounts_own_lines(self, temp_home):
        """v0.29 judges a landing by each account's own lines. #3 sits at 7d
        93% under a 97 weekly line: a usable landing, so the reserve stays out
        of an escape's ranking. Raw headroom against the global 90 would call
        #3 unusable and rank the healthy reserve #2 first, disagreeing with
        the TUI panel's `binding.over` tagging."""
        h = _reserve_harness(temp_home, reserve="2", weekly_threshold=97.0)
        usage = {
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(20, 20, _R_SOON),
            "3": _usage7(10, 93, _R_LATEST),
        }
        headroom = {"1": 0.0, "2": 80.0, "3": 7.0}
        ordered, _, _ = h.engine._rank_reserve_aware(
            trigger="at-limit",
            consume_first=True,
            oauth_candidates=["2", "3"],
            no_return=None,
            usage=usage,
            headroom=headroom,
            current="1",
            active_headroom=0.0,
            settings=h.settings,
            now=h.clock.now,
        )
        assert ordered == ["3"]


# -- 1. reserve last, every trigger -------------------------------------------


class TestReserveRankedLast:
    """#2 is the reserve and the most attractive candidate on every axis
    (most headroom AND soonest weekly reset); the healthy non-reserve #3 must
    win anyway."""

    _PEERS = {
        "2": _usage7(0, 0, _R_SOON),      # reserve: best on every axis
        "3": _usage7(50, 50, _R_LATEST),  # healthy non-reserve
    }

    def test_proactive(self, temp_home):
        h = _reserve_harness(temp_home)
        outcome = h.tick_with_usage({"1": _usage7(92, 20, _R_LATER), **self._PEERS})
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "proactive"
        assert h.active_number() == 3

    def test_at_limit(self, temp_home):
        h = _reserve_harness(temp_home)
        outcome = h.tick_with_usage({"1": _usage7(100, 100, _R_LATER), **self._PEERS})
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "at-limit"
        assert h.active_number() == 3

    def test_failover(self, temp_home):
        h = _reserve_harness(temp_home, unhealthy_ticks=1)
        outcome = h.tick_with_usage({"1": None, **self._PEERS})
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "failover"
        assert h.active_number() == 3

    def test_manual_n(self, temp_home):
        h = _reserve_harness(temp_home)
        h.engine.request_switch()
        outcome = h.tick_with_usage({"1": _usage7(30, 30, _R_LATER), **self._PEERS})
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "manual"
        assert h.active_number() == 3

    def test_all_above_at_limit_escape(self, temp_home):
        """Every account at/over the threshold: today's escape ranking (most
        headroom) would take the reserve #2; reserves still sort last."""
        h = _reserve_harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 100, _R_LATER),
            "2": _usage7(91, 91, _R_SOON),     # reserve, most headroom
            "3": _usage7(97, 97, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_all_above_ranking_puts_reserves_after(self, temp_home):
        h = _reserve_harness(temp_home, reserve="2")
        # Everyone over the line, so the all-above "most room" ranking runs:
        # the reserve #2 has the most room (9 pts) and #3 (7 pts) still clears
        # the margin against the active's 3 (`_all_above_margin_met`), yet
        # the reserve sorts after the non-reserve.
        now = h.clock.now
        usage = {
            "1": _usage75(97, 97, _R_LATER, _iso_at(now + 3 * 3600)),
            "2": _usage75(91, 91, _R_SOON, _iso_at(now + 600)),
            "3": _usage75(93, 93, _R_LATEST, _iso_at(now + 1800)),
        }
        headroom = {n: 100.0 - u["five_hour"]["pct"] for n, u in usage.items()}
        for trigger in ("proactive", "manual", "at-limit", "failover"):
            ordered, _, _ = h.engine._rank_reserve_aware(
                trigger=trigger,
                consume_first=True,
                oauth_candidates=["2", "3"],
                no_return=None,
                usage=usage,
                headroom=headroom,
                current="1",
                active_headroom=None if trigger == "failover" else 3.0,
                settings=h.settings,
                now=h.clock.now,
            )
            assert ordered and ordered[-1] == "2", (trigger, ordered)
            assert ordered[0] == "3", (trigger, ordered)

    def test_warmup_order_puts_reserves_last(self, temp_home):
        """The warmup stagger ranks through the same wrapper."""
        h = _reserve_harness(temp_home)
        usage = {"1": _usage7(30, 30, _R_LATER), **self._PEERS}
        headroom = {n: 100.0 - u["five_hour"]["pct"] for n, u in usage.items()}
        ordered, _, _ = h.engine._rank_reserve_aware(
            trigger="manual",
            consume_first=True,
            oauth_candidates=["2", "3"],
            no_return=None,
            usage=usage,
            headroom=headroom,
            current="1",
            active_headroom=70.0,
            settings=h.settings,
            now=h.clock.now,
        )
        assert ordered == ["3"]


class TestWarmupOrder:
    def test_real_warmup_order_puts_reserves_last(self, temp_home):
        """Through ``_warmup_order`` itself (the stagger's queue): #3 is the
        only ranked target; #2 (reserve, best on every axis) and #4 (at its
        limit) both fall out of the ranking and tie, and the reserve
        tie-break puts #4 first where slot order alone would put #2 first."""
        from claude_swap import warmup

        h = _reserve_harness(temp_home, reserve="2")
        h.seed(4, "d@example.com")
        usage = {
            "1": _usage7(30, 30, _R_LATER),
            "2": _usage7(0, 0, _R_SOON),
            "3": _usage7(50, 50, _R_LATEST),
            "4": _usage7(100, 100, _R_SOON),
        }
        headroom = {n: 100.0 - u["five_hour"]["pct"] for n, u in usage.items()}

        def cold(num: str):
            return warmup.WarmupAccount(
                num, warmup.WindowState(five_hour_cold=True, readable=True)
            )

        candidates = warmup.Candidates(
            eligible=[cold("2"), cold("3"), cold("4")]
        )
        ordered = h.engine._warmup_order(
            candidates, "1", usage, headroom, h.clock.now
        )
        assert [a.number for a in ordered] == ["3", "4", "2"]

        # Same fleet with no reserve: the reserve tie-break is inert and #2,
        # the consume-first favourite, is warmed first.
        h2 = EngineHarness(
            temp_home, strategy=RESERVE_STRATEGY, reserve_accounts="none"
        )
        ordered = h2.engine._warmup_order(
            candidates, "1", usage, headroom, h2.clock.now
        )
        assert [a.number for a in ordered][0] == "2"


class TestEscapePrefersHealthyReserve:
    def test_at_limit_takes_reserve_below_threshold(self, temp_home):
        h = _reserve_harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 100, _R_LATER),
            "2": _usage7(50, 50, _R_LATEST),   # reserve, healthy
            "3": _usage7(95, 95, _R_SOON),     # non-reserve, over threshold
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_proactive_takes_reserve_below_threshold(self, temp_home):
        h = _reserve_harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(92, 92, _R_LATER),
            "2": _usage7(50, 50, _R_LATEST),
            "3": _usage7(95, 95, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_manual_escape_ranks_healthy_reserve_over_unhealthy_non_reserve(
        self, temp_home
    ):
        """`n` with an unreadable active is an escape that ranks by the
        consume-first key, where today the over-threshold #3 (soonest reset,
        cool 5h) would come first. The healthy reserve must lead."""
        h = _reserve_harness(temp_home)
        usage = {
            "1": None,
            "2": _usage7(10, 50, _R_LATEST),   # reserve, healthy, resets last
            "3": _usage7(10, 95, _R_SOON),     # non-reserve, 95% weekly
        }
        headroom = {"1": None, "2": 50.0, "3": 5.0}
        ordered, _, _ = h.engine._rank_reserve_aware(
            trigger="manual",
            consume_first=True,
            oauth_candidates=["2", "3"],
            no_return=None,
            usage=usage,
            headroom=headroom,
            current="1",
            active_headroom=None,
            settings=h.settings,
            now=h.clock.now,
        )
        assert ordered == ["2", "3"]
        # ...and with no reserve configured the same ranking is today's.
        plain, _, _ = h.engine._rank_candidates(
            trigger="manual",
            consume_first=True,
            oauth_candidates=["2", "3"],
            no_return=None,
            usage=usage,
            headroom=headroom,
            current="1",
            active_headroom=None,
            settings=h.settings,
            now=h.clock.now,
        )
        assert plain == ["3", "2"]


# -- 2. never nudged onto ------------------------------------------------------


class TestNudgeNeverLandsOnReserve:
    def test_soonest_reserve_is_ignored(self, temp_home):
        h = _reserve_harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),     # reserve: the consume-first pick
            "3": _usage7(10, 10, _R_LATEST),   # non-reserve, resets later
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["already-consuming-soonest"]

    def test_takes_a_sooner_non_reserve_instead(self, temp_home):
        h = _reserve_harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),     # reserve, soonest
            "3": _usage7(10, 10, _R_LATER),    # sooner than the active
        })
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "consume-first"
        assert h.active_number() == 3

    def test_no_non_reserve_peer_is_below_threshold_not_blocked(self, temp_home):
        """Two accounts, the other a reserve: same no-peer exit as plain
        consume-first (NO_ACTION, never a false BLOCKED)."""
        h = EngineHarness(
            temp_home, strategy=RESERVE_STRATEGY, reserve_accounts="2"
        )
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]


# -- 3. leave the reserve right away -------------------------------------------


class TestLeaveTheReserve:
    """Active #1 is the reserve, below the threshold, resetting SOONEST, so
    plain consume-first would stay put."""

    _USAGE = {
        "1": _usage7(20, 20, _R_SOON),     # reserve active, resets soonest
        "2": _usage7(10, 10, _R_LATER),    # non-reserve, LATER reset
        "3": _usage7(10, 10, _R_LATEST),
    }

    def _h(self, temp_home, **kw) -> EngineHarness:
        return _reserve_harness(temp_home, reserve="1", **kw)

    def test_moves_to_non_reserve_with_later_reset(self, temp_home):
        h = self._h(temp_home)
        outcome = h.tick_with_usage(self._USAGE)
        assert outcome is TickOutcome.SWITCHED
        sw = _switch(h)
        assert sw.trigger == "consume-first"
        assert h.active_number() == 2   # consume-first order among non-reserves
        assert sw.note == "left reserve account #1"
        assert sw.to_json()["note"] == "left reserve account #1"

    def test_dry_run_event_carries_the_note(self, temp_home):
        h = self._h(temp_home)
        h.engine = h._make_engine(dry_run=True)
        assert h.tick_with_usage(self._USAGE) is TickOutcome.SWITCHED
        sw = _switch(h)
        assert sw.dry_run and sw.note == "left reserve account #1"
        assert h.active_number() == 1

    def test_plain_consume_first_would_stay(self, temp_home):
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        assert h.tick_with_usage(self._USAGE) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["already-consuming-soonest"]

    def test_unknown_active_reset_does_not_hold(self, temp_home):
        h = self._h(temp_home)
        usage = dict(self._USAGE, **{"1": _usage7(20, 20)})
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def _two_phase(self, h, stored, fresh):
        from tests.test_autoswitch import TestConsumeFirstStrategy

        return TestConsumeFirstStrategy._two_phase_tick(None, h, stored, fresh)

    def test_two_phase_refetch_disqualifies_stale_pick_and_holds(self, temp_home):
        """The stored snapshot offers #2; the phase-2 refetch shows every
        non-reserve exhausted. The tick re-decides on fresh data and holds
        on the reserve rather than switching on the stale pick."""
        h = self._h(temp_home)
        fresh = dict(
            self._USAGE,
            **{"2": _usage7(100, 100, _R_LATER), "3": _usage7(100, 100, _R_LATEST)},
        )
        outcome, fetch_sets = self._two_phase(h, self._USAGE, fresh)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["reserve-hold"]
        assert fetch_sets.count({"1", "2", "3"}) == 1  # phase 2 fired once

    def test_two_phase_refetch_reranks_off_the_reserve(self, temp_home):
        h = self._h(temp_home)
        fresh = dict(self._USAGE, **{"2": _usage7(100, 100, _R_LATER)})
        outcome, fetch_sets = self._two_phase(h, self._USAGE, fresh)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert {"1", "2", "3"} in fetch_sets

    def test_not_during_cooldown(self, temp_home):
        h = self._h(temp_home)
        h.engine._mutate_state(
            lambda st: st.__setitem__("lastSwitchAt", h.clock.now)
        )
        assert h.tick_with_usage(self._USAGE) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["cooldown"]
        assert h.active_number() == 1

    def test_not_when_no_non_reserve_is_healthy(self, temp_home):
        h = self._h(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_SOON),
            "2": _usage7(95, 95, _R_LATER),
            "3": _usage7(100, 100, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["reserve-hold"]
        assert h.active_number() == 1

    def test_only_other_reserves_is_below_threshold(self, temp_home):
        h = _reserve_harness(temp_home, reserve="1,2,3")
        assert h.tick_with_usage(self._USAGE) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]

    def test_not_under_the_m_pin(self, temp_home):
        """Item 4 too: `m` may pick the reserve, and the pin holds it until
        the threshold."""
        h = self._h(temp_home)
        _on(h, 2, "b@example.com")
        h.engine.request_switch(target="1")
        assert h.tick_with_usage(self._USAGE) is TickOutcome.SWITCHED
        assert h.active_number() == 1
        assert h.engine.pinned == "1"
        h.clock.advance(h.settings.cooldown_seconds + 1)
        h.events.clear()
        assert h.tick_with_usage(self._USAGE) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["pinned"]
        assert h.active_number() == 1

    def test_m_may_pick_a_reserve_over_a_healthy_non_reserve(self, temp_home):
        h = _reserve_harness(temp_home)
        h.engine.request_switch(target="2")
        outcome = h.tick_with_usage({
            "1": _usage7(30, 30, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


# -- empty reserve list == consume-first ---------------------------------------


_PARITY_SCENARIOS = {
    "nudge-sooner": {
        "1": _usage7(20, 20, _R_LATER),
        "2": _usage7(10, 10, _R_SOON),
        "3": _usage7(10, 10, _R_LATEST),
    },
    "stay-soonest": {
        "1": _usage7(20, 20, _R_SOON),
        "2": _usage7(10, 10, _R_LATER),
        "3": _usage7(10, 10, _R_LATEST),
    },
    "proactive": {
        "1": _usage7(95, 20, _R_LATER),
        "2": _usage7(50, 40, _R_SOON),
        "3": _usage7(10, 10, _R_LATEST),
    },
    "at-limit": {
        "1": _usage7(100, 100, _R_LATEST),
        "2": _usage7(30, 95, _R_SOON),
        "3": _usage7(20, 20, _R_LATER),
    },
    "all-above": {
        "1": _usage7(100, 100, _R_LATER),
        "2": _usage7(91, 91, _R_SOON),
        "3": _usage7(97, 97, _R_LATEST),
    },
    "reset-unknown": {
        "1": _usage7(20, 20),
        "2": _usage7(10, 10, _R_SOON),
        "3": _usage7(10, 10, _R_LATEST),
    },
    "failover": {"1": None, "2": _usage7(10, 10, _R_SOON), "3": _usage7(5, 5)},
}


def _run_in_own_home(
    root: Path, strategy: str, usage: dict, manual: bool, reserve: str | None = "none"
):
    home = root / strategy
    (home / ".claude").mkdir(parents=True)
    with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}), \
            patch("pathlib.Path.home", return_value=home):
        h = EngineHarness(
            home, strategy=strategy, unhealthy_ticks=1, reserve_accounts=reserve
        )
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        if manual:
            h.engine.request_switch()
        outcome = h.tick_with_usage(usage)
        events = [
            {k: v for k, v in e.to_json().items() if k != "ts"} for e in h.events
        ]
        return outcome, h.active_number(), events, h.state()


class TestEmptyReserveIsConsumeFirst:
    @pytest.mark.parametrize("manual", [False, True], ids=["auto", "n"])
    @pytest.mark.parametrize("scenario", sorted(_PARITY_SCENARIOS))
    def test_tick_parity(self, tmp_path, scenario, manual):
        usage = _PARITY_SCENARIOS[scenario]
        base = _run_in_own_home(tmp_path, "consume-first", usage, manual)
        ours = _run_in_own_home(tmp_path, RESERVE_STRATEGY, usage, manual)
        assert ours == base

    @pytest.mark.parametrize("scenario", sorted(_PARITY_SCENARIOS))
    def test_default_reserve_is_inert_for_consume_first(self, tmp_path, scenario):
        """The default "1" never reaches plain consume-first."""
        usage = _PARITY_SCENARIOS[scenario]
        base = _run_in_own_home(tmp_path / "a", "consume-first", usage, False)
        ours = _run_in_own_home(
            tmp_path / "b", "consume-first", usage, False, reserve="1"
        )
        assert ours == base

    def test_ranking_parity_randomised(self, temp_home):
        """Every trigger, random fleets: the wrapper with S empty returns
        exactly what `_rank_candidates` does."""
        h = EngineHarness(temp_home, strategy=RESERVE_STRATEGY, reserve_accounts="none")
        assert reserve_accounts(h.settings) == frozenset()
        resets = [None, _R_SOON, _R_LATER, _R_LATEST]
        rng = random.Random(20261101)
        for _ in range(400):
            usage = {
                str(n): _usage7(
                    rng.choice([0, 10, 50, 80, 89, 90, 95, 100]),
                    rng.choice([0, 30, 92, 100]),
                    rng.choice(resets),
                )
                for n in range(1, 6)
            }
            headroom = {
                n: 100.0 - max(u["five_hour"]["pct"], u["seven_day"]["pct"])
                for n, u in usage.items()
            }
            kw = dict(
                trigger=rng.choice(
                    ["proactive", "consume-first", "manual", "at-limit", "failover"]
                ),
                consume_first=True,
                oauth_candidates=["2", "3", "4", "5"],
                no_return=rng.choice([None, "2"]),
                usage=usage,
                headroom=headroom,
                current="1",
                active_headroom=headroom["1"],
                settings=h.settings,
                now=h.clock.now,
            )
            assert h.engine._rank_reserve_aware(**kw) == h.engine._rank_candidates(**kw)


# -- default reserve "1" with no account 1 -------------------------------------


def _run_without_account_1(root: Path, strategy: str, usage: dict, reserve):
    home = root / strategy
    (home / ".claude").mkdir(parents=True)
    with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),             patch("pathlib.Path.home", return_value=home):
        kw = {} if reserve is None else {"reserve_accounts": reserve}
        h = EngineHarness(home, strategy=strategy, unhealthy_ticks=1, **kw)
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.seed(4, "d@example.com")
        h.make_live("b@example.com", 2)
        outcome = h.tick_with_usage(usage)
        events = [
            {k: v for k, v in e.to_json().items() if k != "ts"} for e in h.events
        ]
        return outcome, h.active_number(), events, h.state()


class TestDefaultReserveWithoutAccount1:
    """The default reserve is "1"; a fleet with no account 1 must behave
    exactly like consume-first."""

    @pytest.mark.parametrize(
        "usage",
        [
            {  # nudge to the sooner reset
                "2": _usage7(20, 20, _R_LATER),
                "3": _usage7(10, 10, _R_SOON),
                "4": _usage7(10, 10, _R_LATEST),
            },
            {  # proactive
                "2": _usage7(95, 20, _R_LATER),
                "3": _usage7(50, 40, _R_SOON),
                "4": _usage7(10, 10, _R_LATEST),
            },
            {  # at-limit, all others above
                "2": _usage7(100, 100, _R_LATER),
                "3": _usage7(91, 91, _R_SOON),
                "4": _usage7(97, 97, _R_LATEST),
            },
        ],
        ids=["nudge", "proactive", "all-above"],
    )
    def test_matches_consume_first(self, tmp_path, usage):
        assert AutoSwitchSettings().reserve_accounts == "1"
        base = _run_without_account_1(tmp_path, "consume-first", usage, None)
        ours = _run_without_account_1(tmp_path, RESERVE_STRATEGY, usage, None)
        assert ours == base
