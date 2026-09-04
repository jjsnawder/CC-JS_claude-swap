"""Live auto-switch screen: the real engine, visualized.

Runs :class:`AutoSwitchEngine` in a thread worker and renders its typed
events. Opens in **dry-run** — opening a view must never start switching
accounts on its own; going live is an explicit, confirmed action. The
engine's own state file semantics (shared cooldown, quarantine list, state
lock) make it safe to run alongside an external ``cswap auto``.

The active account's full card sits on top (same widget as the dashboard's
panel, with the threshold tick); this screen adds the engine badge, the
ranked switch candidates, and the decision log. While it is up, the app's
snapshot poller runs store-only: the engine is the only fetcher.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap import oauth
from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    consume_first_key,
    pct_label,
)
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import SETTING_SPECS, load_settings, parse_model_names
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer."""
    role = _EVENT_ROLES.get(event.kind)
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("s", "toggle_strategy", "Strategy"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        # Session-only threshold adjustment (t, then arrows). Never written
        # to settings.json — same memory-only precedent as the dry-run
        # toggle. ``_configured_threshold`` is the mount-time file value the
        # screen reverts to on exit; ``_entry_threshold`` is the value when
        # adjust mode was entered (wake/log only on a net change).
        self._adjusting = False
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None
        # Session-only strategy toggle (s), same memory-only contract as the
        # threshold: ``_configured_strategy`` is the mount-time file value,
        # and the summary tags the difference so a session override can never
        # be mistaken for the persisted one (`cswap config set` is that path).
        self._configured_strategy: str | None = None

    def compose(self) -> ComposeResult:
        yield AccountsPanel(show_minis=False, id="auto-active-panel")
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.app.set_store_only(True)
        self._settings = load_settings(self.app.switcher.backup_dir)
        # The bar tick everywhere reads app.threshold_pct, loaded once at app
        # startup — sync it to the fresh file value so bars and engine agree,
        # and remember that value: unmount restores it (only the session
        # adjustment reverts, not this correction).
        self._configured_threshold = self._settings.threshold
        self._configured_strategy = self._settings.strategy
        self.app.threshold_pct = self._settings.threshold
        self._update_summary()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        self._start_engine(dry_run=True)

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        # A session threshold must not outlive the engine it steered: unpin
        # the poll planner and put the bar tick back on the file value.
        self.app.switcher.clear_poll_policy_inputs()
        if self._configured_threshold is not None:
            self.app.threshold_pct = self._configured_threshold
        self.app.set_store_only(False)

    def _on_theme_change(self, _theme: str) -> None:
        self._update_summary()
        self._update_badge()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def action_back(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self.app.pop_screen()

    # -- threshold adjust mode ------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("threshold_step", "adjust_done") and not self._adjusting:
            return False  # hidden and inert until adjust mode is armed
        if action == "toggle_strategy" and self._adjusting:
            return False  # the keys belong to the threshold while it is armed
        return True

    def action_adjust_threshold(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self._adjusting = True
        self._entry_threshold = self._settings.threshold
        self._update_summary()
        self.refresh_bindings()

    def action_adjust_done(self) -> None:
        if self._adjusting:
            self._end_adjust()

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting:
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        value = min(spec.hi, max(spec.lo, self._settings.threshold + delta))
        self._set_threshold(value)

    def _end_adjust(self) -> None:
        self._adjusting = False
        self._update_summary()
        self.refresh_bindings()
        if self._settings.threshold == self._entry_threshold:
            return  # no net change: nothing to announce, no tick to force
        if self._engine is not None:
            self._engine.wake()  # show a decision at the new value now
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— threshold set to {pct_label(self._settings.threshold)}% "
                "for this session —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _set_threshold(self, value: float) -> None:
        if value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()

    # -- strategy toggle ------------------------------------------------------

    def action_toggle_strategy(self) -> None:
        """Flip best <-> consume-first for this session only.

        Nothing is written to settings.json (`cswap config set
        autoswitch.strategy` stays the persistent path) -- but the engine
        reads its settings at CONSTRUCTION, so the flip only reaches the
        decision by rebuilding it, preserving the dry-run/live state.
        """
        if self._settings is None or self._adjusting:
            return
        # Cycle the configured choices, so a third upstream strategy needs
        # no change here (an unknown value lands on the first choice).
        choices = SETTING_SPECS["autoswitch.strategy"].choices
        current = (
            choices.index(self._settings.strategy)
            if self._settings.strategy in choices
            else -1
        )
        flipped = choices[(current + 1) % len(choices)]
        self._settings = replace(self._settings, strategy=flipped)
        if self._engine is not None:
            self._restart_engine(dry_run=self._engine.dry_run)
        self._update_summary()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)  # "Next best" ranks by the new strategy
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— strategy set to {flipped} for this session —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        text.append("auto-switch · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=palette.accent if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=palette.muted)
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        # The other two decision inputs, on screen: which windows the
        # threshold is measured against, and how the target is chosen.
        models = parse_model_names(self._settings.model)
        text.append(
            f" · model {', '.join(models)}" if models else " · model account-wide"
        )
        text.append(f" · {self._settings.strategy}")
        if self._settings.strategy != self._configured_strategy:
            text.append(" (session)", style=palette.muted)
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"auto-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        log = self.query_one("#event-log", RichLog)
        mode = "DRY-RUN (watching only)" if dry_run else "LIVE (will switch accounts)"
        log.write(
            Text(
                f"— engine started: {mode} —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#event-log", RichLog).write(event_text(event, palette=palette))
        if event.kind == "switch":
            self.app.request_refresh()

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? claude-swap will switch your active account "
                    "automatically when the threshold is reached.\n\n"
                    "(Same behavior as running `cswap auto` in a terminal.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)

    def _restart_engine(self, *, dry_run: bool) -> None:
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#mode-badge", Static)
        if self._engine is not None and not self._engine.dry_run:
            badge.update(" LIVE ")
            badge.set_classes("live")
        else:
            badge.update(" DRY-RUN ")
            badge.set_classes("dry")

    # -- candidates -----------------------------------------------------------

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        self.query_one("#candidates", Static).update(
            self._candidates_text(snap, active_number=snap.active_number)
        )

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets in the order the PROACTIVE trigger would take.

        ``best`` ranks by remaining headroom (least-used first).
        ``consume-first`` ranks by ``autoswitch.consume_first_key`` -- the
        engine's own key, imported rather than reimplemented -- inside a
        health tier that mirrors the engine's landing gate, and annotates
        each row with the weekly reset it ranks on, a ``5h hot`` marker for
        the demoted tier, and a ``skip`` tag on any row the proactive
        trigger would not take right now, so "why did it not move" answers
        itself. Labels and pcts render exactly as under ``best``.

        Scoped to that ONE trigger on purpose, and it is the common case
        rather than the whole engine: an ``at-limit``/``failover`` escape
        ranks by headroom whatever the strategy, and with every account
        at/over the threshold the engine ranks by binding-window recovery
        time instead. Neither is rendered here; a panel claiming to
        predict all three would be wrong two ways.
        """
        # Same window set as the engine (autoswitch.model included), so a
        # displayed pct can never disagree with the one it decides on.
        palette = Palette.from_theme(self.app.current_theme)
        models = parse_model_names(self._settings.model) if self._settings else ()
        consume_first = (
            self._settings is not None
            and self._settings.strategy == "consume-first"
        )
        now = time.time()
        key_kwargs: dict = {}
        threshold = self._settings.threshold if self._settings else 100.0
        active_reset_ts = float("inf")
        active_below = False
        idle = False
        if consume_first:
            key_kwargs = dict(
                threshold=threshold,
                hysteresis_pct=self._settings.hysteresis_pct,
                now=now,
            )
            active = next(
                (a for a in snap.accounts if a.number == active_number), None
            )
            active_usage = active.usage.last_good if active is not None else None
            active_pct = binding_pct(active_usage, models)
            # Read through the same key so "sooner than the active account"
            # inherits its past-is-unknown handling (a stale resets_at must
            # not read as imminent).
            active_reset_ts = consume_first_key(active_usage, None, **key_kwargs)[1]
            # The reset comparison is the BELOW-threshold gate only: from
            # at/over the threshold the engine must move and takes any healthy
            # account, so muting on reset there would contradict it.
            active_below = active_pct is not None and active_pct < threshold
            # ...and below the threshold with the active reset unmeasured, the
            # engine's gate can never pass ("reset-unknown"): nothing here is
            # takeable until that number is reported.
            idle = active_below and active_reset_ts == float("inf")
        # (sort key, number) with a TUPLE key, so `best`'s single pct and
        # consume-first's tiered key share one list. Unreadable rows keep
        # their 998/999 buckets and sort after every keyed row.
        ranked: list[tuple[tuple, str]] = []
        lines: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            pct = binding_pct(acc.usage.last_good, models)
            entry = Text()
            entry.append(f"\n  {acc.number:>2}  ", style=palette.foreground)
            entry.append(acc.email, style=palette.foreground)
            # A row the proactive trigger would not take right now gets a
            # trailing muted `skip` tag; the label and pct render exactly as
            # under `best`, so the panel's colours mean the same thing
            # whichever key ordered it.
            skip = False
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                ranked.append(((998.0,), acc.number))
            elif pct is None:
                entry.append("  usage unknown", style=palette.muted)
                ranked.append(((999.0,), acc.number))
            elif consume_first:
                entry.append(f"  {pct:3.0f}% used", style=palette.severity(pct))
                key = consume_first_key(
                    acc.usage.last_good,
                    # The engine's own headroom, not `100 - pct`: identical
                    # today and immune to a future binding_pct that measures
                    # something the headroom does not.
                    oauth.account_headroom(acc.usage.last_good, models),
                    **key_kwargs,
                )
                resets = data.window_reset_text(
                    acc.usage.last_good, "seven_day", now
                )
                if resets is not None:
                    entry.append(f" · {resets}", style=palette.muted)
                if key[0]:
                    entry.append("  5h hot", style=palette.muted)
                # The engine's landing gate runs BEFORE its key: a candidate
                # at/over the threshold re-triggers on the next tick, so it is
                # never a proactive target however soon its week resets. The
                # panel tiers on the same test rather than only greying the
                # row — untiered, an unhealthy account still rendered first
                # and read as the engine's next pick.
                unhealthy = pct >= threshold
                # Tagged, still RANKED: this is the order the engine would
                # use the moment the account becomes eligible.
                skip = unhealthy or (active_below and key[1] >= active_reset_ts)
                ranked.append(((0.0, 1 if unhealthy else 0) + key, acc.number))
            else:
                entry.append(f"  {pct:3.0f}% used", style=palette.severity(pct))
                ranked.append(((pct,), acc.number))
            if skip or idle:  # idle: nothing here is takeable
                entry.append("  skip", style=palette.muted)
            lines[acc.number] = entry

        text = Text()
        text.append("Next best", style=palette.muted)
        if not ranked:
            text.append("\n  no other switchable accounts", style=palette.muted)
            return text
        if idle:
            text.append(
                "\n  consume-first idle until the active account's weekly "
                "reset is reported",
                style=palette.muted,
            )
        # Key only: `ranked` is in snapshot (sequence) order and sort is
        # stable, so a full tie falls through to sequence order exactly as the
        # engine's does. Sorting the pairs compared the account NUMBER as a
        # string on ties, where "10" precedes "2".
        for _key, number in sorted(ranked, key=lambda t: t[0]):
            text.append(lines[number])
        return text
