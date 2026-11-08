"""Fork (pin-control): ``release_pin`` and the switch-source log lines.

docs/plans/2026-11-08-pin-control.md. Engine-side only; the control op and
the TUI key are in tests/test_engine_control.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.menubar import _SWITCH_LOG_RE

import tests.test_autoswitch as _ta
from tests.test_autoswitch import (  # noqa: F401
    EngineHarness,
    _R_LATER,
    _R_LATEST,
    _R_SOON,
    _usage7,
    harness,
)

# Module access, not a name import: a Test* class bound here is re-collected.
_CF_USAGE = _ta.TestManualPick._CF_USAGE  # consume-first: #2 resets soonest


def _cf(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home, strategy="consume-first")
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _pin_on_3(h: EngineHarness, source: str | None = "tui-m") -> None:
    h.engine.request_switch(target="3", source=source)
    assert h.tick_with_usage(_CF_USAGE) is TickOutcome.SWITCHED
    assert h.engine.pinned == "3"
    h.clock.advance(h.settings.cooldown_seconds + 1)
    h.events.clear()


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _fork_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "claude-swap" and r.getMessage().startswith("auto-switch:")
    ]


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO, logger="claude-swap")
    return caplog


class TestReleasePin:
    def test_release_clears_wakes_and_next_tick_runs_consume_first(self, temp_home):
        h = _cf(temp_home)
        _pin_on_3(h)
        assert h.tick_with_usage(_CF_USAGE) is TickOutcome.NO_ACTION
        assert _reasons(h) == ["pinned"]
        h.events.clear()
        h.engine._wake.clear()

        assert h.engine.release_pin("tui-u") == "3"
        assert h.engine.pinned is None
        assert h.engine._wake.is_set()

        assert h.tick_with_usage(_CF_USAGE) is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "consume-first"
        assert h.active_number() == 2  # resets sooner than #3

    def test_release_without_a_hold_is_none_and_does_not_wake(self, harness):
        harness.engine._wake.clear()
        assert harness.engine.release_pin("engine-unpin") is None
        assert not harness.engine._wake.is_set()

    def test_release_before_a_queued_pick_lands_ends_with_the_picks_pin(
        self, temp_home
    ):
        """Latest action wins: the pick lands after the release."""
        h = _cf(temp_home)
        h.engine.request_switch(target="3", source="tui-m")
        assert h.engine.release_pin("tui-u") is None
        assert h.tick_with_usage(_CF_USAGE) is TickOutcome.SWITCHED
        assert h.engine.pinned == "3"

    def test_release_mid_switch_loses_to_the_landing_pick(self, temp_home):
        """A release racing a pick already inside its tick: the old hold is
        released, then the pick lands and pins its own target."""
        h = _cf(temp_home)
        _pin_on_3(h)
        h.engine.request_switch(target="2", source="engine-pick")
        real = h.switcher.switch_to
        released = []

        def switch_mid_release(*a, **kw):
            released.append(h.engine.release_pin("engine-unpin"))
            return real(*a, **kw)

        with patch.object(h.switcher, "switch_to", side_effect=switch_mid_release):
            assert h.tick_with_usage(_CF_USAGE) is TickOutcome.SWITCHED
        assert released == ["3"]
        assert h.engine.pinned == "2"

    def test_never_persisted(self, temp_home):
        h = _cf(temp_home)
        _pin_on_3(h)
        h.engine.release_pin("tui-u")
        assert "pinned" not in str(h.state())


class TestSwitchSourceLog:
    def test_tui_m_logs_the_switch_and_the_pin(self, temp_home, logs):
        h = _cf(temp_home)
        _pin_on_3(h, source="tui-m")
        lines = _fork_lines(logs)
        assert "auto-switch: engine moved #1 -> #3 (trigger manual, source tui-m)" in lines
        assert "auto-switch: holding #3 (pin set by tui-m)" in lines

    def test_tui_n_logs_its_source(self, temp_home, logs):
        h = _cf(temp_home)
        h.engine.request_switch(source="tui-n")
        assert h.tick_with_usage(_CF_USAGE) is TickOutcome.SWITCHED
        assert any(
            "(trigger manual, source tui-n)" in ln for ln in _fork_lines(logs)
        )
        assert h.engine.pinned is None

    def test_engine_pick_logs_its_source(self, temp_home, logs):
        h = _cf(temp_home)
        h.engine.request_switch(target="3", on_result=lambda o, e: None,
                                source="engine-pick")
        h.tick_with_usage(_CF_USAGE)
        lines = _fork_lines(logs)
        assert any("(trigger manual, source engine-pick)" in ln for ln in lines)
        assert "auto-switch: holding #3 (pin set by engine-pick)" in lines

    def test_a_request_without_source_is_labelled_request(self, temp_home, logs):
        h = _cf(temp_home)
        h.engine.request_switch(target="3")
        h.tick_with_usage(_CF_USAGE)
        assert any("source request)" in ln for ln in _fork_lines(logs))

    def test_autonomous_consume_first_is_source_engine(self, temp_home, logs):
        h = _cf(temp_home)
        _pin_on_3(h)
        h.engine.release_pin("tui-u")
        h.tick_with_usage(_CF_USAGE)
        lines = _fork_lines(logs)
        assert "auto-switch: hold on #3 released (tui-u)" in lines
        assert (
            "auto-switch: engine moved #3 -> #2 (trigger consume-first, source engine)"
            in lines
        )

    def test_threshold_release_and_at_limit_escape(self, temp_home, logs):
        h = _cf(temp_home)
        h.engine = h._make_engine(pinned="1")
        h.tick_with_usage({
            "1": _usage7(100, 100, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        lines = _fork_lines(logs)
        assert "auto-switch: hold on #1 released (threshold reached)" in lines
        assert any("(trigger at-limit, source engine)" in ln for ln in lines)

    def test_moved_off_the_pick_is_logged_once(self, temp_home, logs):
        h = _cf(temp_home)
        _pin_on_3(h)
        h.switcher.switch_to("1", json_output=True)  # outside the engine
        h.tick_with_usage(_CF_USAGE)
        h.tick_with_usage(_CF_USAGE)
        moved = [ln for ln in _fork_lines(logs) if "moved off the pick" in ln]
        assert moved == ["auto-switch: hold on #3 released (moved off the pick)"]

    def test_n_replacing_a_pin_is_logged(self, temp_home, logs):
        h = _cf(temp_home)
        _pin_on_3(h)
        h.engine.request_switch(source="tui-n")
        assert h.tick_with_usage(_CF_USAGE) is TickOutcome.SWITCHED
        assert h.engine.pinned is None
        assert "auto-switch: hold on #3 released (replaced by tui-n)" in _fork_lines(logs)

    def test_dry_run_logs_no_switch_line(self, temp_home, logs):
        h = _cf(temp_home)
        h.engine = h._make_engine(dry_run=True)
        h.engine.request_switch(target="3", source="tui-m")
        h.tick_with_usage(_CF_USAGE)
        assert not any("engine moved" in ln for ln in _fork_lines(logs))

    def test_no_new_line_matches_the_menubar_regex_or_carries_an_email(
        self, temp_home, logs
    ):
        h = _cf(temp_home)
        _pin_on_3(h)
        h.engine.release_pin("tui-u")
        h.tick_with_usage(_CF_USAGE)
        h.clock.advance(h.settings.cooldown_seconds + 1)
        h.engine.request_switch(source="tui-n")
        h.tick_with_usage(_CF_USAGE)
        lines = _fork_lines(logs)
        assert len(lines) >= 5
        for ln in lines:
            assert not _SWITCH_LOG_RE.search(ln), ln
            assert "@" not in ln, ln
        # upstream's own line is still there, untouched
        assert any(
            _SWITCH_LOG_RE.search(r.getMessage()) for r in logs.records
        )
