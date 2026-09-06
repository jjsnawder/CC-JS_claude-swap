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

import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap import oauth, warmup
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


_USED_RE = re.compile(r"\): (\d+% used) ")


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer.

    The clock stamp is always foreground so key times stand out of a scrolling
    log; the body is muted unless the event kind carries a role colour
    (switch, error, quarantine, exhausted). A poll line's active-account
    ``N% used`` is lifted to foreground too — the one number worth reading.
    """
    role = _EVENT_ROLES.get(event.kind)
    if event.kind == "warmup" and getattr(event, "action", "") == "failed":
        # Warmup is background noise until it stops working: a hello that
        # cannot run means an account's window will keep lapsing unseen.
        role = "sev_warn"
    style = getattr(palette, role) if role is not None else palette.muted
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.foreground)
    body = event.human()
    match = _USED_RE.search(body) if event.kind == "poll" else None
    if match is None:
        text.append(body, style=style)
        return text
    text.append(body[: match.start(1)], style=style)
    text.append(match.group(1), style=palette.foreground)
    text.append(body[match.end(1) :], style=style)
    return text


def countdown_text(seconds: float) -> str:
    """``H:MM:SS`` from an hour up, ``MM:SS`` below it. Never negative."""
    s = int(max(0.0, seconds))
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _clock_at(ts: float) -> str:
    """Local-time ``HH:MM`` for an absolute instant."""
    return time.strftime("%H:%M", time.localtime(ts))


def _slot_status_text(slot: "warmup.WarmupSlot", *, now: float) -> tuple[str, str]:
    """One slot's status phrase, plus the palette role it defaults to."""
    if slot.upcoming:
        if slot.at_ts is not None and slot.at_ts > now:
            return (
                f"next hello in {countdown_text(slot.at_ts - now)} "
                f"({_clock_at(slot.at_ts)}) · {slot.model}",
                "foreground",
            )
        # A dry-run engine previews the same decision every tick and spawns
        # nothing — "due now" forever would read as a stuck warmup.
        if slot.dry_run:
            return f"would hello now (dry-run) · {slot.model}", "foreground"
        return f"hello due now · {slot.model}", "foreground"
    if slot.status == "in-flight":
        return "hello in flight", "accent"
    if slot.status == "warm":
        if slot.reset_ts is not None:
            return (
                f"warm · 5h resets {_clock_at(slot.reset_ts)} "
                f"(in {countdown_text(slot.reset_ts - now)})",
                "muted",
            )
        return "warm", "muted"
    if slot.status == "excluded":
        return f"held: {slot.reason or 'switched to this tick'}", "muted"
    return f"skipped: {slot.reason or 'not eligible'}", "muted"


def warmup_panel_text(
    schedule: dict,
    *,
    now: float,
    enabled: bool,
    palette: Palette = Palette.DARK,
    stale_after_s: float | None = None,
) -> Text:
    """The warmup panel: when every managed account is next warmed.

    Pure — takes the engine's :meth:`AutoSwitchEngine.warmup_schedule`
    snapshot and a clock, returns the rendered block. The soonest upcoming
    hello is summarized on the header line and its row is accented, so "when
    do 1/2/3 get warmed" is answerable at a glance rather than by reading
    back through the event log.

    ``stale_after_s`` guards the one failure this panel cannot otherwise
    show: the engine's spawn phase swallows its own exceptions, so a pass
    that dies mid-flight leaves the PREVIOUS pass's schedule standing and
    its countdowns run to "due now" and sit there. Past that age (the
    caller's poll interval with room to spare) the header says so rather
    than presenting arithmetic on a dead plan as fact.
    """
    text = Text()
    text.append("WARMUP", style=f"bold {palette.accent}")
    if not enabled:
        text.append(
            "  off — cswap config set autoswitch.warmupEnabled true",
            style=palette.muted,
        )
        return text
    slots = list(schedule.values())
    if not slots:
        text.append("\n  waiting for first tick…", style=palette.muted)
        return text
    # The soonest hello still ahead of us: a `due` row (this tick) outranks
    # any scheduled one, and among scheduled rows the earliest `at_ts` wins.
    upcoming = [s for s in slots if s.upcoming]
    soonest = min(
        upcoming,
        key=lambda s: (s.at_ts if s.status == "scheduled" and s.at_ts else 0.0),
        default=None,
    )
    in_flight = sum(1 for s in slots if s.status == "in-flight")
    if soonest is not None and soonest.at_ts is not None and soonest.at_ts > now:
        text.append(
            f"  ·  next: Account-{soonest.number} in "
            f"{countdown_text(soonest.at_ts - now)}",
            style=palette.muted,
        )
    elif soonest is not None:
        text.append(f"  ·  next: Account-{soonest.number} due now", style=palette.muted)
    elif in_flight:
        text.append(
            f"  ·  {in_flight} hello{'s' if in_flight > 1 else ''} in flight",
            style=palette.muted,
        )
    else:
        text.append("  ·  nothing scheduled", style=palette.muted)
    if stale_after_s is not None:
        computed = max((s.computed_ts for s in slots), default=0.0)
        if computed and now - computed > stale_after_s:
            text.append(" (stale)", style=palette.muted)
    for slot in slots:
        text.append(f"\n  {slot.number:>2}  ", style=palette.foreground)
        if slot.email:
            text.append(slot.email, style=palette.foreground)
        status, role = _slot_status_text(slot, now=now)
        if soonest is not None and slot.number == soonest.number:
            role = "accent"
        text.append("  ")
        text.append(status, style=getattr(palette, role))
    return text


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("s", "toggle_strategy", "Strategy"),
        Binding("n", "switch_now", "Switch now"),
        Binding("p", "ping_warm", "Ping/warm"),
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
        self._warm_timer = None
        # Last warmup panel text rendered, to skip no-op repaints.
        self._warm_rendered: str | None = None

    def compose(self) -> ComposeResult:
        yield AccountsPanel(show_minis=False, id="auto-active-panel")
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
            yield Static("", id="warmup-panel")
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
        # A countdown has to move on its own: the engine only speaks on a
        # tick (up to a minute apart) and the panel's whole job is telling
        # you how long until the next hello. Screen-owned, so it stops with
        # the screen; the render is a dict copy plus a few strings.
        self._warm_timer = self.set_interval(1.0, self._refresh_warmup_panel)
        self._refresh_warmup_panel()

    def on_unmount(self) -> None:
        if self._warm_timer is not None:
            self._warm_timer.stop()
            self._warm_timer = None
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
        # Same text, different colours: the repaint cache must not swallow a
        # theme change.
        self._warm_rendered = None
        self._refresh_warmup_panel()
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
        if action in ("toggle_strategy", "switch_now", "ping_warm") and self._adjusting:
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

    # -- switch now -----------------------------------------------------------

    def action_switch_now(self) -> None:
        """Ask the engine to switch on its next tick (trigger ``manual``).

        The ENGINE performs it, not this screen: only the engine's tick does
        the health checks, the token freshening, the quarantine handling and
        the state record that a hand-rolled ``switch_to`` here would skip. It
        ranks by the strategy shown in the summary — the session override
        included, since `s` rebuilds the engine — without the anti-flap
        margins, so it takes the strategy's top HEALTHY candidate: the
        margin-based `skip` tags in "Next best" are waived, but a row
        at/over the threshold is not (landing there re-triggers at once).

        Inert in threshold-adjust mode, like `s`: the keys belong to the
        threshold while it is armed. No confirmation — in DRY-RUN this is a
        preview (`[dry-run] would switch ...`) and going LIVE was already
        confirmed once, by `l`.
        """
        if self._settings is None or self._adjusting or self._engine is None:
            return
        self._engine.request_switch()
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— switch now requested ({self._settings.strategy}) —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    # -- ping / warm ----------------------------------------------------------

    def action_ping_warm(self) -> None:
        """Ask the engine to warm every cold account on its next tick.

        Like `n`, the ENGINE does it: the hello has to go through the slot
        preparation, the eligibility predicates and the state file, none of
        which this screen owns. Unlike the automatic warmup, the request
        ignores the stagger and does not require ``warmupEnabled`` — the
        keypress is the authorization, and the point of it is to fill in the
        reset stamps that an Anthropic-side reset left blank.

        Inert in threshold-adjust mode, like `s` and `n`. In DRY-RUN the
        engine previews (``would warm ...``) instead of spawning.
        """
        if self._settings is None or self._adjusting or self._engine is None:
            return
        self._engine.request_warm()
        self.query_one("#event-log", RichLog).write(
            Text(
                "— warmup requested —",
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
        # A `pinged`/`failed`/`would-ping` line and the panel must agree the
        # moment it lands, not up to a second later.
        self._refresh_warmup_panel()
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
        # The new engine has planned nothing yet: show that rather than the
        # dead one's schedule.
        self._refresh_warmup_panel()

    # -- warmup panel ---------------------------------------------------------

    def _refresh_warmup_panel(self) -> None:
        """Re-render the countdown block from the engine's schedule.

        Called on a 1s timer, on every engine event, and on a theme change.
        Reads only ``warmup_schedule()`` — a copy taken under the engine's
        own lock — so it never touches engine state from this thread.
        """
        if not self.is_attached:
            return
        engine = self._engine
        schedule = engine.warmup_schedule() if engine is not None else {}
        # The ENGINE's setting, not the screen's mount-time copy: `s` and `l`
        # rebuild the engine, and that rebuild is where a changed setting
        # takes effect. `p` (request_warm) also plans without
        # `warmupEnabled`, so a schedule on hand outranks the setting —
        # never hide a live plan behind an "off" line.
        enabled = bool(schedule) or bool(
            engine is not None and engine.warmup_enabled
        )
        interval = (
            self._settings.interval_seconds if self._settings is not None else 60.0
        )
        text = warmup_panel_text(
            schedule,
            now=time.time(),
            enabled=enabled,
            palette=Palette.from_theme(self.app.current_theme),
            # Two polls: one missed pass is a hiccup, two is a dead plan.
            stale_after_s=2.0 * interval,
        )
        # A second-by-second countdown is the only thing that normally
        # changes here; when even that is static (nothing scheduled, warmup
        # off) skip the update so an idle screen does not repaint at 1 Hz.
        if text.plain == self._warm_rendered:
            return
        self._warm_rendered = text.plain
        self.query_one("#warmup-panel", Static).update(text)

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
