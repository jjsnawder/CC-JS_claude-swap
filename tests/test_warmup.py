"""Tests for the warmup module (warmup.py).

**No test here may spawn a real ``claude``.** The runner is the one seam
that starts a process, and every test that reaches it patches
``claude_swap.warmup.subprocess.Popen``; every test above that seam injects
a fake :class:`~claude_swap.warmup.PingRunner`. A real child would escape
``tests/conftest.py``'s real-store audit hook entirely (it guards THIS
process's writes, not a grandchild's) and would authenticate as whoever is
logged in on the machine running the suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_swap import warmup
from claude_swap.usage_store import UsageEntry
from claude_swap.warmup import (
    DEFAULT_MODEL,
    PERIOD_S,
    PingResult,
    SubprocessPingRunner,
    WarmupAccount,
    WindowState,
    plan_warmups,
    warm_now,
    window_state,
)

NOW = 1_700_000_000.0
MINUTE = 60.0


def iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def usage(
    five_reset: float | None = None,
    *,
    five_pct: float = 10.0,
    seven_pct: float = 10.0,
    scoped: list[tuple[str, float, float | None]] | None = None,
) -> dict:
    five: dict = {"pct": five_pct}
    if five_reset is not None:
        five["resets_at"] = iso(five_reset)
    out: dict = {"five_hour": five, "seven_day": {"pct": seven_pct}}
    if scoped is not None:
        out["scoped"] = [
            {"name": name, "pct": pct, **({"resets_at": iso(ts)} if ts else {})}
            for name, pct, ts in scoped
        ]
    return out


# ---------------------------------------------------------------------------
# Cold detection
# ---------------------------------------------------------------------------


class TestWindowState:
    def test_absent_stamp_is_cold(self):
        state = window_state(usage(None), NOW)
        assert state.five_hour_cold is True
        assert state.reset_ts is None
        assert state.readable is True

    def test_past_stamp_is_cold(self):
        """A stamp that has elapsed means the window ran out and nothing has
        used the account since — indistinguishable from never-used, and
        exactly as cold."""
        state = window_state(usage(NOW - 1), NOW)
        assert state.five_hour_cold is True
        assert state.reset_ts is None

    def test_future_stamp_is_warm_and_carries_the_phase(self):
        state = window_state(usage(NOW + 3600), NOW)
        assert state.five_hour_cold is False
        assert state.reset_ts == pytest.approx(NOW + 3600)

    def test_missing_five_hour_key_entirely_is_cold(self):
        assert window_state({"seven_day": {"pct": 1.0}}, NOW).five_hour_cold is True

    def test_unreadable_usage_is_never_cold(self):
        """A sentinel or an unknown reads as NOT cold: an account whose usage
        cannot be read must never be pinged (the alternative is spending a
        token to learn what a fetch would have told us for free)."""
        for value in (None, "token-expired", 42):
            state = window_state(value, NOW)
            assert state.readable is False
            assert state.five_hour_cold is False

    def test_at_limit_when_any_relevant_window_is_full(self):
        assert window_state(usage(NOW + 60, seven_pct=100.0), NOW).at_limit is True
        assert window_state(usage(NOW + 60, five_pct=100.0), NOW).at_limit is True
        assert window_state(usage(NOW + 60), NOW).at_limit is False

    def test_scoped_model_window_cold_only_when_configured(self):
        u = usage(NOW + 60, scoped=[("Fable", 5.0, None)])
        assert window_state(u, NOW).cold_models == ()
        assert window_state(u, NOW, ("Fable",)).cold_models == ("Fable",)

    def test_scoped_model_window_with_a_future_stamp_is_warm(self):
        u = usage(NOW + 60, scoped=[("Fable", 5.0, NOW + 99)])
        assert window_state(u, NOW, ("Fable",)).cold_models == ()

    def test_scoped_model_window_with_a_past_stamp_is_cold(self):
        u = usage(NOW + 60, scoped=[("Fable", 5.0, NOW - 1)])
        assert window_state(u, NOW, ("Fable",)).cold_models == ("Fable",)

    def test_all_matches_every_scoped_window(self):
        u = usage(
            NOW + 60,
            scoped=[("Fable", 5.0, None), ("Opus", 5.0, NOW + 99), ("Haiku", 1.0, None)],
        )
        assert window_state(u, NOW, ("all",)).cold_models == ("Fable", "Haiku")

    def test_a_scoped_window_named_like_an_account_window_is_not_confused(self):
        """``relevant_windows`` returns 5h/7d first and the label slice is
        positional, so a per-model display name of "5h" cannot make the
        account-wide window look like a model window."""
        u = usage(NOW + 60, scoped=[("5h", 5.0, None)])
        state = window_state(u, NOW, ("all",))
        assert state.cold_models == ("5h",)
        assert state.five_hour_cold is False

    def test_at_limit_counts_configured_model_windows_too(self):
        u = usage(NOW + 60, scoped=[("Fable", 100.0, NOW + 99)])
        assert window_state(u, NOW).at_limit is False
        assert window_state(u, NOW, ("Fable",)).at_limit is True


class TestModelAlias:
    def test_display_name_lowercases(self):
        assert warmup.model_alias("Fable") == "fable"
        assert warmup.model_alias("Opus") == "opus"

    def test_blank_falls_back_to_the_default(self):
        assert warmup.model_alias("  ") == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def cold(number: str, *, models: tuple[str, ...] = ()) -> WarmupAccount:
    return WarmupAccount(
        number,
        WindowState(five_hour_cold=True, cold_models=models, readable=True),
    )


def warm(number: str, reset_ts: float, *, models: tuple[str, ...] = ()) -> WarmupAccount:
    return WarmupAccount(
        number,
        WindowState(
            five_hour_cold=False,
            reset_ts=reset_ts,
            cold_models=models,
            readable=True,
        ),
    )


def by_number(decisions) -> dict:
    return {d.number: d for d in decisions}


class TestPlanWarmups:
    def test_the_worked_case_four_cold_accounts_spread_75_minutes_apart(self):
        """The plan's worked example, pinned: four cold accounts at 08:00
        warm at 08:00 / 09:15 / 10:30 / 11:45, so their new 5-hour windows
        reset at 13:00 / 14:15 / 15:30 / 16:45 — 75 minutes apart, which is
        exactly ``period / N``.

        The CHAIN still produces those instants out of order (#2 takes the
        antipode of the only phase that exists, #3 halves the first of the
        two equal gaps that leaves, #4 halves the remaining big one) — but
        the instants are sorted before they are handed out, and the caller's
        order is the priority order, so the first account listed gets the
        earliest one.
        """
        accounts = [cold(str(n)) for n in (1, 2, 3, 4)]
        decisions = by_number(plan_warmups(NOW, accounts))

        assert decisions["1"].action == "ping_now"
        waits = {
            n: decisions[n].at_ts - NOW for n in ("2", "3", "4")
        }
        assert decisions["2"].action == "wait_until"
        assert waits["2"] == pytest.approx(75 * MINUTE)
        assert waits["3"] == pytest.approx(150 * MINUTE)
        assert waits["4"] == pytest.approx(225 * MINUTE)

        resets = sorted(
            [NOW + PERIOD_S] + [NOW + w + PERIOD_S for w in waits.values()]
        )
        gaps = [b - a for a, b in zip(resets, resets[1:])]
        assert gaps == pytest.approx([75 * MINUTE] * 3)

    def test_a_window_that_lapses_on_phase_pings_immediately(self):
        """Steady state. Three accounts are warm on a perfect 100-minute
        spread; the fourth's window just lapsed at its own slot, so
        ``now + period`` already lands on the target midpoint."""
        spacing = PERIOD_S / 4
        accounts = [
            warm("1", NOW + PERIOD_S - spacing),
            warm("2", NOW + PERIOD_S - 2 * spacing),
            warm("3", NOW + PERIOD_S - 3 * spacing),
            cold("4"),
        ]
        assert by_number(plan_warmups(NOW, accounts))["4"].action == "ping_now"

    def test_small_drift_inside_the_tolerance_still_pings_now(self):
        """The LEGACY default (``tolerance_s=None`` → half a spacing), which
        no caller uses any more: the engine always passes a tick-sized
        tolerance. Kept because the fallback is still in the signature —
        half a spacing is far too wide to be "close enough" in production,
        and `test_a_hair_outside_the_tick_tolerance_waits` is the rule that
        actually runs."""
        spacing = PERIOD_S / 4
        drift = spacing / 2 - 60  # just inside tolerance
        accounts = [
            warm("1", NOW + PERIOD_S - spacing + drift),
            warm("2", NOW + PERIOD_S - 2 * spacing + drift),
            warm("3", NOW + PERIOD_S - 3 * spacing + drift),
            cold("4"),
        ]
        assert by_number(plan_warmups(NOW, accounts))["4"].action == "ping_now"

    def test_large_drift_outside_the_tolerance_waits(self):
        spacing = PERIOD_S / 4
        drift = spacing / 2 + 600  # comfortably outside
        accounts = [
            warm("1", NOW + PERIOD_S - spacing + drift),
            warm("2", NOW + PERIOD_S - 2 * spacing + drift),
            warm("3", NOW + PERIOD_S - 3 * spacing + drift),
            cold("4"),
        ]
        decision = by_number(plan_warmups(NOW, accounts))["4"]
        assert decision.action == "wait_until"
        assert decision.at_ts == pytest.approx(NOW + drift)

    def test_two_accounts_aim_at_the_antipode(self):
        """With one warm peer resetting a full period out, the cold account
        waits half a period so the two end up 2h30 apart."""
        decision = by_number(
            plan_warmups(NOW, [warm("1", NOW + PERIOD_S), cold("2")])
        )["2"]
        assert decision.action == "wait_until"
        assert decision.at_ts == pytest.approx(NOW + PERIOD_S / 2)

    def test_fewer_eligible_accounts_widen_the_tolerance(self):
        """Also the LEGACY default, and the reason it was replaced: N is the
        eligible COUNT, so under ``tolerance_s=None`` the same 50-minute
        drift is a wait at N=4 (37m30) and a ping at N=2 (1h15) — a
        tolerance that tracks fleet size rather than tick cadence, which is
        what fired an account 37 minutes early in the field. Pinned as the
        fallback's behaviour, not as the intended one."""
        drift = 50 * MINUTE
        s4 = PERIOD_S / 4
        four = [
            warm("1", NOW + PERIOD_S - s4 + drift),
            warm("2", NOW + PERIOD_S - 2 * s4 + drift),
            warm("3", NOW + PERIOD_S - 3 * s4 + drift),
            cold("4"),
        ]
        assert by_number(plan_warmups(NOW, four))["4"].action == "wait_until"

        two = [warm("1", NOW + PERIOD_S / 2 + drift), cold("2")]
        assert by_number(plan_warmups(NOW, two))["2"].action == "ping_now"

    def test_stagger_off_is_plain_keep_alive(self):
        accounts = [cold(str(n)) for n in (1, 2, 3, 4)]
        decisions = plan_warmups(NOW, accounts, stagger=False)
        assert [d.action for d in decisions] == ["ping_now"] * 4
        assert {d.reason for d in decisions} == {"keep-alive"}

    def test_a_cold_model_window_picks_the_model_for_the_hello(self):
        """One call starts both windows, so a cold account that also has a
        cold model window spends its hello on that model instead of haiku."""
        decisions = by_number(plan_warmups(NOW, [cold("1", models=("Fable",))]))
        assert decisions["1"].action == "ping_now"
        assert decisions["1"].model == "fable"

    def test_a_warm_account_with_a_cold_model_window_pings_at_once(self):
        """Its 5-hour window is already running, so a hello cannot disturb
        the phase — there is nothing to wait for."""
        decisions = by_number(
            plan_warmups(
                NOW,
                [warm("1", NOW + 3600, models=("Fable",)), cold("2")],
            )
        )
        assert decisions["1"].action == "ping_now"
        assert decisions["1"].model == "fable"
        assert decisions["1"].reason == "model-window-cold"

    def test_a_fully_warm_account_gets_no_decision_at_all(self):
        assert plan_warmups(NOW, [warm("1", NOW + 3600)]) == []

    def test_no_accounts_no_decisions(self):
        assert plan_warmups(NOW, []) == []

    # -- the tolerance -------------------------------------------------------
    #
    # The tolerance answers ONE question: "is the next tick close enough to
    # the target that waiting for it buys nothing?". It is therefore a poll
    # interval wide, not a share of the spacing — and it is asymmetric.
    # Fixtures below use the antipode property: with a single warm peer at
    # ``NOW + P/2 + off`` the cold account's target instant is ``NOW + off``,
    # so ``off`` IS the signed wait (positive = early, negative = late).

    @staticmethod
    def _one_peer(off: float, tolerance_s: float | None = 120.0):
        return by_number(
            plan_warmups(
                NOW,
                [warm("1", NOW + PERIOD_S / 2 + off), cold("2")],
                tolerance_s=tolerance_s,
            )
        )["2"]

    def test_inside_the_tick_tolerance_pings_now(self):
        assert self._one_peer(119.0).action == "ping_now"

    def test_a_hair_outside_the_tick_tolerance_waits(self):
        """The live bug: at ``spacing / 2`` (1h15 at N=2) this pinged, and
        the account's window started an hour before its target."""
        decision = self._one_peer(121.0)
        assert decision.action == "wait_until"
        assert decision.at_ts == pytest.approx(NOW + 121.0)
        # ...and the old, wide default is what let it through.
        assert self._one_peer(121.0, tolerance_s=None).action == "ping_now"

    def test_the_tolerance_can_never_exceed_half_a_spacing(self):
        """A poll interval longer than the spacing must not turn the
        tolerance into "ping whenever": the cap keeps the phase within half
        a spacing of its target however the engine is configured."""
        spacing = PERIOD_S / 2
        assert self._one_peer(spacing / 2 + 60, tolerance_s=PERIOD_S).action == (
            "wait_until"
        )

    def test_being_late_pings_now_rather_than_waiting_a_period(self):
        """An overslept loop (machine sleep, a long tick) is past the target
        by definition. Waiting for the phase to come round again would cost
        hours for an account that is cold right now."""
        assert self._one_peer(-60.0).action == "ping_now"
        assert self._one_peer(-(PERIOD_S / 4) + 60).action == "ping_now"

    def test_late_by_more_than_half_a_spacing_takes_the_next_slot(self):
        """The late side is bounded, and the bound is load-bearing: a
        ping-now lands its phase at ``now + period``, so allowing it further
        than half a spacing from the target lets two accounts land on the
        SAME phase and the day ends up with fewer windows than accounts."""
        spacing = PERIOD_S / 2
        decision = self._one_peer(-(spacing / 2) - 60)
        assert decision.action == "wait_until"

    def test_no_two_planned_phases_coincide(self):
        """The property the bound exists to protect, from a cold start."""
        for n in (3, 4, 5):
            accounts = [cold(str(i)) for i in range(1, n + 1)]
            decisions = plan_warmups(NOW, accounts, tolerance_s=120.0)
            phases = {
                round((NOW if d.at_ts is None else d.at_ts) + PERIOD_S)
                for d in decisions
            }
            assert len(phases) == n

    def test_a_target_far_behind_us_does_not_land_on_a_peers_phase(self):
        """The case the late bound exists for, built directly: two warm
        peers leave a gap whose midpoint sits well over half a spacing
        BEHIND ``now + period``. Unbounded, the cold account would ping now
        and its window would start on top of a peer's; bounded, it takes the
        next slot instead."""
        spacing = PERIOD_S / 3
        # Peers at +P/12 and +P/2 from `now + period`: the largest gap runs
        # from +P/2 round to +P/12, and its midpoint lands 62m30 BEHIND us —
        # past half a spacing (50 minutes at N=3), so the slot is missed.
        peers = [
            warm("1", NOW + PERIOD_S + PERIOD_S / 12),
            warm("2", NOW + PERIOD_S + PERIOD_S / 2),
        ]
        decisions = plan_warmups(NOW, peers + [cold("3")], tolerance_s=120.0)
        decision = by_number(decisions)["3"]
        assert decision.action == "wait_until"
        assert decision.at_ts - NOW > spacing / 2
        phases = {round(p.state.reset_ts % PERIOD_S) for p in peers}
        assert round((decision.at_ts + PERIOD_S) % PERIOD_S) not in phases

    # -- who gets which instant ----------------------------------------------

    def test_the_caller_order_is_the_priority_order(self):
        """The engine passes its own switch ranking, so "the account you are
        most likely to switch to next" is the one warmed first. Same instants
        whichever order they arrive in — only the pairing moves."""
        instants = []
        for order in (("1", "2", "3", "4"), ("4", "3", "2", "1")):
            decisions = by_number(
                plan_warmups(NOW, [cold(n) for n in order], tolerance_s=120.0)
            )
            first = decisions[order[0]]
            assert first.action == "ping_now"  # earliest instant, always
            waits = [
                decisions[n].at_ts - NOW for n in order[1:]
            ]
            assert waits == sorted(waits)  # ascending, in caller order
            instants.append(sorted([0.0] + waits))
        assert instants[0] == pytest.approx(instants[1])

    def test_each_account_keeps_its_own_model_pick(self):
        decisions = by_number(
            plan_warmups(
                NOW,
                [cold("1"), cold("2", models=("Fable",))],
                tolerance_s=120.0,
            )
        )
        assert decisions["1"].model == DEFAULT_MODEL
        assert decisions["2"].model == "fable"

    def test_a_scheduled_account_counts_as_warm_for_the_next_one(self):
        """Two cold accounts must not be planned into the same slot: #2's
        planned phase is what #3 measures its gap against."""
        decisions = by_number(plan_warmups(NOW, [cold("1"), cold("2"), cold("3")]))
        planned = {
            "1": NOW,
            "2": decisions["2"].at_ts,
            "3": decisions["3"].at_ts,
        }
        assert len({round(v) for v in planned.values()}) == 3


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


SUCCESS_JSON = json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 2100,
    "result": "OK",
    "total_cost_usd": 0.0008,
    "usage": {
        "input_tokens": 431,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 79,
    },
})


class FakePopen:
    """Stands in for ``subprocess.Popen``; records exactly how it was called."""

    calls: list[dict] = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = self.rc
        self.killed = False
        FakePopen.calls.append({"argv": argv, **kwargs})

    # class-level knobs the tests set
    rc = 0
    stdout = SUCCESS_JSON
    stderr = ""
    timeout_first = False

    def communicate(self, timeout=None):
        if self.timeout_first and not self.killed:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.calls = []
    FakePopen.rc = 0
    FakePopen.stdout = SUCCESS_JSON
    FakePopen.stderr = ""
    FakePopen.timeout_first = False
    monkeypatch.setattr(warmup.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(warmup.shutil, "which", lambda name: "/usr/bin/claude")
    return FakePopen


class TestSubprocessPingRunner:
    def test_the_command_line_is_exactly_the_measured_one(self, fake_popen, tmp_path):
        """This argv IS the contract (decisions.md, 2026-09-06). Dropping
        ``--tools ""``/``--setting-sources ""`` alone took a measured hello
        from ~430 input tokens to 21,626 cache-creation tokens — $0.0008 to
        $0.044. Any change here is a cost change."""
        SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert fake_popen.calls[0]["argv"] == [
            "/usr/bin/claude",
            "-p", "hi",
            "--model", "haiku",
            "--max-turns", "1",
            "--no-session-persistence",
            "--output-format", "json",
            "--system-prompt", "Reply OK.",
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--no-chrome",
            "--max-budget-usd", "0.10",
        ]

    def test_the_model_alias_is_spliced_in(self, fake_popen, tmp_path):
        SubprocessPingRunner().ping(tmp_path / "cfg", "fable", tmp_path)
        argv = fake_popen.calls[0]["argv"]
        assert argv[argv.index("--model") + 1] == "fable"

    def test_auth_overrides_are_scrubbed_and_the_config_dir_is_explicit(
        self, fake_popen, tmp_path, monkeypatch
    ):
        """An exported API key would make the hello bill a key instead of
        starting the ACCOUNT's window, and an inherited CLAUDE_CONFIG_DIR
        would point every account's hello at this process's own profile."""
        for var in warmup.AUTH_OVERRIDE_ENV_VARS:
            monkeypatch.setenv(var, "leak")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
        monkeypatch.setenv("PATH_MARKER", "kept")
        cfg = tmp_path / "slot-1"
        SubprocessPingRunner().ping(cfg, "haiku", tmp_path)
        env = fake_popen.calls[0]["env"]
        for var in warmup.AUTH_OVERRIDE_ENV_VARS:
            assert var not in env
        assert env["CLAUDE_CONFIG_DIR"] == str(cfg)
        assert env["PATH_MARKER"] == "kept"

    def test_stdin_is_closed_and_the_cwd_is_ours(self, fake_popen, tmp_path):
        SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path / "wd")
        call = fake_popen.calls[0]
        assert call["stdin"] is subprocess.DEVNULL
        assert call["cwd"] == str(tmp_path / "wd")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows spawn flags")
    def test_windows_hides_the_window_and_drops_priority(self, fake_popen, tmp_path):
        """No console may flash on the dev box, and a warmup must never
        compete with the work the machine is actually doing."""
        SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        flags = fake_popen.calls[0]["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.BELOW_NORMAL_PRIORITY_CLASS
        assert "start_new_session" not in fake_popen.calls[0]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX spawn flags")
    def test_posix_detaches_the_session(self, fake_popen, tmp_path):
        SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert fake_popen.calls[0]["start_new_session"] is True
        assert "creationflags" not in fake_popen.calls[0]

    def test_success_json_is_parsed_into_tokens_and_cost(self, fake_popen, tmp_path):
        result = SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert result.ok is True
        assert result.input_tokens == 431
        assert result.output_tokens == 79
        assert result.cost_usd == pytest.approx(0.0008)

    def test_cache_creation_tokens_count_as_input(self, fake_popen, tmp_path):
        """The regression that costs 50x is invisible unless cache-creation
        tokens are folded into the reported input count."""
        fake_popen.stdout = json.dumps({
            "subtype": "success",
            "is_error": False,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 21626,
                      "output_tokens": 64},
        })
        result = SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert result.input_tokens == 21_636

    def test_is_error_true_is_a_failure_even_with_rc_zero(self, fake_popen, tmp_path):
        fake_popen.stdout = json.dumps({"subtype": "success", "is_error": True})
        assert SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path).ok is False

    def test_non_json_output_is_a_failure_not_a_crash(self, fake_popen, tmp_path):
        fake_popen.stdout = "Usage: claude [options]"
        result = SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert result.ok is False
        assert "JSON" in result.error

    def test_a_nonzero_exit_reports_the_code_and_a_truncated_stderr(
        self, fake_popen, tmp_path
    ):
        fake_popen.rc = 1
        # Spaced words, so the base64-ish redaction (runs of 40+ token
        # characters) does not swallow the sample before the cap is reached.
        fake_popen.stderr = "oops " * 1000
        result = SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert result.ok is False
        assert result.error.startswith("rc=1: ")
        assert len(result.error) == len("rc=1: ") + warmup.STDERR_CAP

    def test_child_stderr_is_redacted_before_it_is_quoted(
        self, fake_popen, tmp_path
    ):
        """The child's stderr is the one useful diagnostic a failed hello
        leaves, so it is quoted — which makes it the one place a token or a
        slot path could ride out of this module into a log, a JSON payload
        or a pasted issue. Over-eager on purpose: a false positive costs a
        word, a false negative costs a rotation."""
        fake_popen.rc = 1
        fake_popen.stderr = (
            "OAuth error: token sk-ant-oat01-AAAAbbbbCCCC rejected; "
            "header 'Bearer eyJhbGciOi.J9.sig'; "
            "profile /home/j/.claude-swap-backup/sessions/2-b-at-example.com "
            "blob QWxhZGRpbjpvcGVuIHNlc2FtZQQWxhZGRpbjpvcGVuIHNlc2FtZQ=="
        )
        error = SubprocessPingRunner().ping(
            tmp_path / "cfg", "haiku", tmp_path
        ).error
        assert "sk-ant-" not in error
        assert "eyJhbGciOi" not in error
        assert "sessions/2-b" not in error
        assert "QWxhZGRpbjpvcGVu" not in error
        assert "OAuth error" in error  # the useful part survives

    def test_redaction_helper_is_also_capped(self):
        assert warmup.redact_child_output("sk-ant-oat01-abcdefgh") == "<token>"
        assert warmup.redact_child_output("Bearer abc.def") == "<token>"
        assert warmup.redact_child_output("at ~/.claude/settings.json") == (
            "at <path>"
        )
        assert len(warmup.redact_child_output("word " * 500)) == warmup.STDERR_CAP

    def test_safe_error_never_carries_a_filename(self, tmp_path):
        """``str(OSError)`` appends the filename — for us a slot path."""
        err = PermissionError(13, "Permission denied", str(tmp_path / "slot-1"))
        assert warmup.safe_error(err) == "PermissionError: Permission denied"
        assert warmup.safe_error(RuntimeError(f"boom {tmp_path}")) == "RuntimeError"

    def test_a_timeout_kills_the_child_by_its_own_handle(self, fake_popen, tmp_path):
        fake_popen.timeout_first = True
        result = SubprocessPingRunner().ping(
            tmp_path / "cfg", "haiku", tmp_path, timeout_s=5
        )
        assert result.ok is False
        assert "timed out" in result.error
        # Our own Popen handle, never a name-based kill.
        assert FakePopen.calls and result.model == "haiku"

    def test_no_claude_on_path_is_a_clean_failure(self, fake_popen, tmp_path, monkeypatch):
        monkeypatch.setattr(warmup.shutil, "which", lambda name: None)
        result = SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert result.ok is False
        assert "not found on PATH" in result.error
        assert not fake_popen.calls

    def test_a_result_never_carries_our_env_or_the_config_path(
        self, fake_popen, tmp_path, monkeypatch
    ):
        """A failure line is logged, scrolled and pasted into issues. The
        config dir names a slot (and, on the active account, the live
        login), and the environment can hold anything — neither may reach
        the payload."""
        monkeypatch.setenv("SECRET_MARKER", "s3cret-value")
        fake_popen.rc = 3
        fake_popen.stderr = "claude: authentication failed"
        cfg = tmp_path / "slot-1-someone-at-example.com"
        result = SubprocessPingRunner().ping(cfg, "haiku", tmp_path)
        blob = repr(result)
        assert "CLAUDE_CONFIG_DIR" not in blob
        assert str(cfg) not in blob
        assert "slot-1-someone" not in blob
        assert "s3cret-value" not in blob
        # ...but the child's OWN stderr is the accepted surface: it is what
        # names the cause, quoted verbatim and capped.
        assert result.error == "rc=3: claude: authentication failed"

    def test_the_quoted_stderr_is_capped_at_two_hundred_characters(
        self, fake_popen, tmp_path
    ):
        fake_popen.rc = 3
        fake_popen.stderr = "error " * 2000
        result = SubprocessPingRunner().ping(tmp_path / "cfg", "haiku", tmp_path)
        assert result.error == "rc=3: " + ("error " * 2000).strip()[:200]


# ---------------------------------------------------------------------------
# warm_now (the manual path used by `cswap warm` and the dashboard)
# ---------------------------------------------------------------------------


class RecordingRunner:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[Path, str]] = []

    def ping(self, config_dir, model, cwd, timeout_s=warmup.PING_TIMEOUT_S):
        self.calls.append((Path(config_dir), model))
        return PingResult(ok=self.ok, model=model, error="" if self.ok else "nope")


class FakeSwitcher:
    def __init__(self, tmp_path: Path, entries: dict[str, UsageEntry], active="1"):
        self.backup_dir = tmp_path / "backup"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._entries = entries
        self._active = active
        self.kinds: dict[str, str] = {}
        self.fetches: list[set] = []
        #: Set to a SwitchError to simulate running inside a `cswap run` shell.
        self.session_shell_error: Exception | None = None

    def _refuse_session_shell(self) -> None:
        if self.session_shell_error is not None:
            raise self.session_shell_error

    def switchable_account_numbers(self):
        return sorted(self._entries)

    def account_kind_for(self, number):
        return self.kinds.get(number, "oauth")

    def account_email(self, number):
        return f"{number}@example.com"

    def current_account_number(self):
        return self._active

    def usage_entries_by_account(self, fetch=None, scheduled=False):
        if fetch:
            self.fetches.append(set(fetch))
        return self._entries


@pytest.fixture
def no_slot_bootstrap(monkeypatch, tmp_path):
    """``config_dir_for`` would run the real slot bootstrap. Faked: these
    tests are about which accounts get pinged, not about session setup."""
    monkeypatch.setattr(
        warmup, "config_dir_for", lambda sw, num, active: tmp_path / f"cfg-{num}"
    )


class TestWarmNow:
    def _switcher(self, tmp_path, **states) -> FakeSwitcher:
        entries = {
            num: UsageEntry(last_good=value, fetched_at=NOW, age_s=0.0)
            for num, value in states.items()
        }
        return FakeSwitcher(tmp_path, entries)

    def test_only_cold_accounts_are_pinged(self, tmp_path, no_slot_bootstrap):
        sw = self._switcher(tmp_path, **{"1": usage(None), "2": usage(NOW + 3600)})
        runner = RecordingRunner()
        summary = warm_now(sw, runner=runner, now=NOW)
        assert [c[1] for c in runner.calls] == ["haiku"]
        assert {r.number: r.action for r in summary.rows} == {
            "1": "pinged", "2": "skipped"
        }
        assert summary.exit_code() == 0

    def test_force_all_pings_the_warm_ones_too(self, tmp_path, no_slot_bootstrap):
        sw = self._switcher(tmp_path, **{"1": usage(None), "2": usage(NOW + 3600)})
        runner = RecordingRunner()
        summary = warm_now(sw, runner=runner, now=NOW, force_all=True)
        assert len(runner.calls) == 2
        assert summary.attempted == 2

    def test_dry_run_spawns_nothing(self, tmp_path, no_slot_bootstrap):
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        runner = RecordingRunner()
        summary = warm_now(sw, runner=runner, now=NOW, dry_run=True)
        assert runner.calls == []
        assert [r.action for r in summary.rows] == ["would-ping"]
        assert summary.exit_code() == 0

    def test_nothing_to_do_is_exit_code_two(self, tmp_path, no_slot_bootstrap):
        sw = self._switcher(tmp_path, **{"1": usage(NOW + 3600)})
        summary = warm_now(sw, runner=RecordingRunner(), now=NOW)
        assert summary.attempted == 0
        assert summary.exit_code() == 2

    def test_a_failure_is_exit_code_one(self, tmp_path, no_slot_bootstrap):
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        summary = warm_now(sw, runner=RecordingRunner(ok=False), now=NOW)
        assert summary.failures == 1
        assert summary.exit_code() == 1

    def test_pinged_accounts_are_refetched(self, tmp_path, no_slot_bootstrap):
        """The whole point of the manual path is to SEE the new stamp."""
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        warm_now(sw, runner=RecordingRunner(), now=NOW)
        assert sw.fetches == [{"1"}]

    def test_api_key_and_at_limit_accounts_are_never_pinged(
        self, tmp_path, no_slot_bootstrap
    ):
        sw = self._switcher(
            tmp_path,
            **{"1": usage(None), "2": usage(None), "3": usage(None, seven_pct=100.0)},
        )
        sw.kinds["2"] = "api_key"
        runner = RecordingRunner()
        warm_now(sw, runner=runner, now=NOW)
        assert len(runner.calls) == 1

    def test_the_active_account_uses_the_live_config_home(self, tmp_path, monkeypatch):
        """The active account goes through the live config home. Nothing
        downstream enforces that: ``setup_session`` does NOT refuse the
        active login (the refusal lives in ``SessionManager.run``, which
        warmup never calls), so this `number == active` comparison IS the
        guard against building a slot copy of the live login."""
        live = tmp_path / "live-claude"
        monkeypatch.setattr(warmup.paths, "get_claude_config_home", lambda: live)
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        runner = RecordingRunner()
        warm_now(sw, runner=runner, now=NOW)
        assert runner.calls[0][0] == live

    def test_a_non_active_account_goes_through_setup_session(
        self, tmp_path, monkeypatch
    ):
        """...and through NO other path: the slot dir must come from the
        exact ``cswap run`` preparation, never a hand-built one. ``share`` is
        True deliberately — ``_sync_sharing(share=False)`` REMOVES managed
        shared items and unlinks the manifest, which would strip
        settings.json/skills/agents/CLAUDE.md out of a slot a live ``cswap
        run`` session is using."""
        seen: list[tuple[str, bool]] = []

        class FakeSessionManager:
            def __init__(self, switcher):
                pass

            def setup_session(self, identifier, share):
                seen.append((identifier, share))
                return tmp_path / f"slot-{identifier}", identifier, "x@example.com"

        import claude_swap.session as session_mod

        monkeypatch.setattr(session_mod, "SessionManager", FakeSessionManager)
        sw = self._switcher(tmp_path, **{"1": usage(NOW + 3600), "2": usage(None)})
        runner = RecordingRunner()
        warm_now(sw, runner=runner, now=NOW)
        assert seen == [("2", True)]  # share=True: see the docstring
        assert runner.calls[0][0] == tmp_path / "slot-2"

    def test_a_broken_slot_fails_only_its_own_account(self, tmp_path, monkeypatch):
        def boom(sw, num, active):
            if num == "2":
                raise RuntimeError("bootstrap exploded")
            return tmp_path / "cfg"

        monkeypatch.setattr(warmup, "config_dir_for", boom)
        sw = self._switcher(tmp_path, **{"1": usage(None), "2": usage(None)})
        summary = warm_now(sw, runner=RecordingRunner(), now=NOW)
        actions = {r.number: r.action for r in summary.rows}
        assert actions == {"1": "pinged", "2": "failed"}

    def test_a_recent_hello_is_not_repeated_inside_the_cooldown(
        self, tmp_path, no_slot_bootstrap
    ):
        """The post-hello refetch cannot beat the store's serve TTL, so a
        just-warmed account still READS cold. Without the cooldown, back-to-
        back `cswap warm` runs would each spend a fresh hello."""
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        runner = RecordingRunner()
        warm_now(sw, runner=runner, now=NOW)
        assert len(runner.calls) == 1
        warm_now(sw, runner=runner, now=NOW + 60)
        assert len(runner.calls) == 1
        warm_now(sw, runner=runner, now=NOW + warmup.PING_COOLDOWN_S + 1)
        assert len(runner.calls) == 2

    def test_force_all_overrides_the_cooldown(self, tmp_path, no_slot_bootstrap):
        """`--all` is the explicit override; the user asked by name."""
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        runner = RecordingRunner()
        warm_now(sw, runner=runner, now=NOW)
        warm_now(sw, runner=runner, now=NOW + 60, force_all=True)
        assert len(runner.calls) == 2

    def test_the_manual_path_honours_and_records_the_model_day_guard(
        self, tmp_path, no_slot_bootstrap
    ):
        """A Fable hello is noise against a weekly window but it is still a
        Fable call: repeated `cswap warm` runs must not re-spend one, so the
        manual path reads and writes the SAME state the engine does."""
        sw = self._switcher(
            tmp_path,
            **{"1": usage(NOW + 3600, scoped=[("Fable", 1.0, None)])},
        )
        runner = RecordingRunner()
        warm_now(sw, models=("Fable",), runner=runner, now=NOW)
        assert [model for _, model in runner.calls] == ["fable"]
        assert warmup.read_warmup_state(sw)["1"]["modelPings"]["Fable"] == NOW

        # Past the ping cooldown, but well inside the day guard.
        later = NOW + warmup.PING_COOLDOWN_S + 60
        warm_now(sw, models=("Fable",), runner=runner, now=later)
        assert len(runner.calls) == 1

        after_a_day = NOW + warmup.MODEL_PING_INTERVAL_S + 1
        warm_now(sw, models=("Fable",), runner=runner, now=after_a_day)
        assert [model for _, model in runner.calls] == ["fable", "fable"]

    def test_a_slot_preparation_failure_never_leaks_a_path(
        self, tmp_path, monkeypatch
    ):
        """`setup_session` messages can embed slot and config-dir paths."""
        def boom(sw, num, active):
            raise RuntimeError(f"could not bootstrap {tmp_path / 'slot-1-secret'}")

        monkeypatch.setattr(warmup, "config_dir_for", boom)
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        summary = warm_now(sw, runner=RecordingRunner(), now=NOW)
        assert summary.rows[0].action == "failed"
        assert "slot-1-secret" not in summary.rows[0].detail
        assert summary.rows[0].detail.startswith("RuntimeError")

    def test_a_session_shell_refuses_to_warm_at_all(
        self, tmp_path, no_slot_bootstrap
    ):
        """Inside a `cswap run N` shell the "active" account is the
        SESSION's, so the machine's real default login reads as just another
        candidate and would be handed to `setup_session` — a slot copy of
        the live login, rotating the backup while ~/.claude keeps the
        predecessor. Upstream refuses every live-store mutation from such a
        shell; a warmup rotates credentials, so it is one."""
        from claude_swap.exceptions import SwitchError

        sw = self._switcher(tmp_path, **{"1": usage(None)})
        sw.session_shell_error = SwitchError("This shell is inside a cswap run")
        runner = RecordingRunner()
        with pytest.raises(SwitchError):
            warm_now(sw, runner=runner, now=NOW)
        assert runner.calls == []

    def test_the_warmup_cwd_is_created_under_the_backup_root(
        self, tmp_path, no_slot_bootstrap
    ):
        sw = self._switcher(tmp_path, **{"1": usage(None)})
        warm_now(sw, runner=RecordingRunner(), now=NOW)
        assert (sw.backup_dir / warmup.WARMUP_DIRNAME).is_dir()


class TestLiveHelloRegistry:
    """The process-wide registry the switch paths consult.

    The engine's own ``_warm_inflight`` dies with the engine; the dangerous
    sequence is cross-surface (`p` on the auto screen, `escape` back to the
    dashboard — which stops the engine while the daemon thread runs on —
    then `enter` to switch onto that slot). Hence a registry owned by the
    process.
    """

    def test_empty_by_default(self):
        assert warmup.active_hellos() == frozenset()

    def test_registered_for_the_duration_and_released_after(self):
        with warmup.hello_in_flight("2"):
            assert warmup.active_hellos() == {"2"}
        assert warmup.active_hellos() == frozenset()

    def test_released_even_when_the_hello_raises(self):
        with pytest.raises(RuntimeError):
            with warmup.hello_in_flight("2"):
                raise RuntimeError("boom")
        assert warmup.active_hellos() == frozenset()

    def test_overlapping_hellos_are_counted_not_flattened(self):
        """Two surfaces may legitimately overlap on one account; the first
        to finish must not clear the flag for the other."""
        with warmup.hello_in_flight("2"):
            with warmup.hello_in_flight("2"):
                assert warmup.active_hellos() == {"2"}
            assert warmup.active_hellos() == {"2"}
        assert warmup.active_hellos() == frozenset()

    def test_numbers_are_normalized_to_strings(self):
        with warmup.hello_in_flight(2):
            assert warmup.active_hellos() == {"2"}

    def test_perform_hello_registers_across_the_slot_preparation(
        self, tmp_path, monkeypatch
    ):
        """The registration must span ``config_dir_for`` too, not just the
        child process: the slot PREPARATION is where the refresh token
        rotates, so a registry that started at ``runner.ping`` would leave
        the dangerous half unguarded."""
        seen: list[frozenset] = []

        def spying_config_dir(sw, num, active):
            seen.append(warmup.active_hellos())
            return tmp_path / "cfg"

        monkeypatch.setattr(warmup, "config_dir_for", spying_config_dir)
        runner = RecordingRunner()
        sw = FakeSwitcher(tmp_path, {})
        warmup.perform_hello(sw, "2", "haiku", "1", runner, tmp_path)
        assert seen == [frozenset({"2"})]
        assert warmup.active_hellos() == frozenset()

    def test_a_failed_preparation_still_releases(self, tmp_path, monkeypatch):
        def boom(sw, num, active):
            raise RuntimeError("no slot")

        monkeypatch.setattr(warmup, "config_dir_for", boom)
        with pytest.raises(RuntimeError):
            warmup.perform_hello(
                FakeSwitcher(tmp_path, {}), "2", "haiku", "1",
                RecordingRunner(), tmp_path,
            )
        assert warmup.active_hellos() == frozenset()

    def test_the_switcher_refuses_to_activate_a_slot_being_warmed(self):
        """The real chokepoint: ``_perform_switch`` covers every surface —
        `cswap switch`, the dashboard, the rotate/best strategies and the
        engine — not just the one that happens to know about warmup."""
        from claude_swap.exceptions import SwitchError
        from claude_swap.switcher import ClaudeAccountSwitcher

        # Reads nothing off `self`; called unbound to keep the test off the
        # real store.
        guard = ClaudeAccountSwitcher._refuse_hello_in_flight
        with warmup.hello_in_flight("2"):
            with pytest.raises(SwitchError, match="warmup ping in flight"):
                guard(None, "2")
            guard(None, "3")  # a different slot is unaffected
        guard(None, "2")  # ...and it clears

    def test_warm_now_registers_its_pool_hellos(self, tmp_path, monkeypatch):
        seen: list[frozenset] = []

        class WatchingRunner(RecordingRunner):
            def ping(self, config_dir, model, cwd, timeout_s=warmup.PING_TIMEOUT_S):
                seen.append(warmup.active_hellos())
                return super().ping(config_dir, model, cwd, timeout_s)

        monkeypatch.setattr(
            warmup, "config_dir_for", lambda sw, num, active: tmp_path / "cfg"
        )
        entries = {
            "1": UsageEntry(last_good=usage(None), fetched_at=NOW, age_s=0.0)
        }
        warm_now(FakeSwitcher(tmp_path, entries), runner=WatchingRunner(), now=NOW)
        assert seen == [frozenset({"1"})]
        assert warmup.active_hellos() == frozenset()


class TestCollectCandidates:
    def _sw(self, tmp_path, entries) -> FakeSwitcher:
        return FakeSwitcher(tmp_path, entries)

    def test_a_stale_cold_row_is_deferred_not_pinged(self, tmp_path):
        """Never spend a token on a stale read: the stamp may have landed
        since the last fetch."""
        stale = UsageEntry(
            last_good=usage(None), fetched_at=NOW - 240, age_s=240.0
        )
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": stale}), {"1": stale}, NOW
        )
        assert out.eligible == []
        assert out.stale == ["1"]

    def test_a_stale_warm_row_is_not_deferred(self, tmp_path):
        """Only rows that would trigger a hello need freshness."""
        stale = UsageEntry(
            last_good=usage(NOW + 3600), fetched_at=NOW - 240, age_s=240.0
        )
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": stale}), {"1": stale}, NOW
        )
        assert out.stale == []
        assert [a.number for a in out.eligible] == ["1"]

    def test_a_cooling_down_account_holds_its_phase_too(self, tmp_path):
        """Same reasoning as in-flight: it is about to be warm, so the other
        cold accounts must plan AROUND it, not into its slot."""
        entry = UsageEntry(last_good=usage(None), fetched_at=NOW, age_s=0.0)
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": entry}),
            {"1": entry},
            NOW,
            warmup_state={"1": {"lastPingAt": NOW - 60}},
        )
        assert out.skipped["1"] == "ping-cooldown"
        assert out.eligible[0].state.reset_ts == pytest.approx(
            NOW - 60 + PERIOD_S
        )
        assert plan_warmups(NOW, out.eligible) == []

    def test_the_cooldown_lifts(self, tmp_path):
        entry = UsageEntry(last_good=usage(None), fetched_at=NOW, age_s=0.0)
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": entry}),
            {"1": entry},
            NOW,
            warmup_state={
                "1": {"lastPingAt": NOW - warmup.PING_COOLDOWN_S - 1}
            },
        )
        assert "1" not in out.skipped
        assert out.eligible[0].state.five_hour_cold is True

    def test_an_in_flight_account_holds_its_phase_instead_of_vanishing(
        self, tmp_path
    ):
        """Dropping it from E would shrink N and let the next cold account
        bootstrap into the slot this hello is already claiming."""
        entry = UsageEntry(last_good=usage(None), fetched_at=NOW, age_s=0.0)
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": entry}), {"1": entry}, NOW, in_flight={"1"}
        )
        assert out.skipped["1"] == "in-flight"
        assert [a.number for a in out.eligible] == ["1"]
        held = out.eligible[0].state
        assert held.five_hour_cold is False
        assert held.reset_ts == pytest.approx(NOW + PERIOD_S)
        assert plan_warmups(NOW, out.eligible) == []

    def test_warmup_backoff_holds_an_account_out(self, tmp_path):
        entry = UsageEntry(last_good=usage(None), fetched_at=NOW, age_s=0.0)
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": entry}),
            {"1": entry},
            NOW,
            warmup_state={"1": {"backoffUntil": NOW + 60}},
        )
        assert out.skipped["1"] == "warmup-backoff"

    def test_a_recent_model_ping_is_not_repeated_for_a_day(self, tmp_path):
        entry = UsageEntry(
            last_good=usage(NOW + 3600, scoped=[("Fable", 1.0, None)]),
            fetched_at=NOW,
            age_s=0.0,
        )
        sw = self._sw(tmp_path, {"1": entry})
        fresh = warmup.collect_candidates(sw, {"1": entry}, NOW, models=("Fable",))
        assert fresh.eligible[0].state.cold_models == ("Fable",)
        guarded = warmup.collect_candidates(
            sw,
            {"1": entry},
            NOW,
            models=("Fable",),
            warmup_state={"1": {"modelPings": {"Fable": NOW - 3600}}},
        )
        assert guarded.eligible[0].state.cold_models == ()

    def test_the_guard_expires_after_a_day(self, tmp_path):
        entry = UsageEntry(
            last_good=usage(NOW + 3600, scoped=[("Fable", 1.0, None)]),
            fetched_at=NOW,
            age_s=0.0,
        )
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": entry}),
            {"1": entry},
            NOW,
            models=("Fable",),
            warmup_state={
                "1": {"modelPings": {"Fable": NOW - warmup.MODEL_PING_INTERVAL_S - 1}}
            },
        )
        assert out.eligible[0].state.cold_models == ("Fable",)

    def test_quarantined_and_token_dead_accounts_are_skipped(self, tmp_path):
        live = UsageEntry(last_good=usage(None), fetched_at=NOW, age_s=0.0)
        dead = UsageEntry(
            last_good=usage(None), fetched_at=NOW, age_s=0.0, auth_dead_strikes=99
        )
        entries = {"1": live, "2": live, "3": dead}
        out = warmup.collect_candidates(
            self._sw(tmp_path, entries), entries, NOW, quarantined={"2"}
        )
        assert [a.number for a in out.eligible] == ["1"]
        assert out.skipped["2"] == "quarantined"
        assert out.skipped["3"] == "token-dead"

    def test_a_fetch_backed_off_account_is_skipped(self, tmp_path):
        entry = UsageEntry(
            last_good=usage(None), fetched_at=NOW, age_s=0.0, backoff_until=NOW + 300
        )
        out = warmup.collect_candidates(
            self._sw(tmp_path, {"1": entry}), {"1": entry}, NOW
        )
        assert out.skipped["1"] == "fetch-backoff"

    def test_quarantined_numbers_reads_the_state_file(self, tmp_path):
        sw = FakeSwitcher(tmp_path, {})
        assert warmup.quarantined_numbers(sw) == set()
        (sw.backup_dir / "autoswitch_state.json").write_text(
            json.dumps({"quarantine": {"2": {"reason": "dead"}}}), encoding="utf-8"
        )
        assert warmup.quarantined_numbers(sw) == {"2"}

    def test_a_corrupt_state_file_quarantines_nobody(self, tmp_path):
        sw = FakeSwitcher(tmp_path, {})
        (sw.backup_dir / "autoswitch_state.json").write_text("{[", encoding="utf-8")
        assert warmup.quarantined_numbers(sw) == set()
