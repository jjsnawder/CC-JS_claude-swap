"""Fork (cswap fleet): the Hosts screen — the dev box's view of its workers.

One row per worker host: follow state (following / overridden / autonomous
and why), the worker's active slot, lease age, reachability, re-login slots
and settings drift. Keys on the highlighted row set the worker's override
(``f`` follow, ``p`` pin, ``s`` strategy) in ``hosts.json`` through
``fleet_remote.set_override``, push this box's settings to it (``u``), or
pull fresh state over ssh (``r``).

Data: while an engine on this box leads (Ruling 4: a LIVE engine with hosts
configured), the leader's last per-host snapshots are re-read every few
seconds — no ssh from this screen. Otherwise the screen pulls once on open
and again only on ``r``. Every read runs in a thread worker: ``fleet_status``
can sit on an ssh timeout and the UI loop never waits on it.

Overrides only steer a worker while this box leads (Ruling 6); the header
says which case is in force. Accounts are slot numbers only — no emails.
"""

from __future__ import annotations

import time
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from claude_swap import fleet_remote
from claude_swap.settings import SELECTABLE_STRATEGIES
from claude_swap.tui.data import format_duration
from claude_swap.tui.modals import AccountPickModal, ConfirmModal, OutputModal, PickRow
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

LEADER_REFRESH_S = 5.0
_NOT_REPORTED = "not reported by the leader yet (r pulls over ssh)"


# --------------------------------------------------------------------------
# Route (the app.py hunk calls these)
# --------------------------------------------------------------------------


def hosts_configured(app: Any) -> bool:
    """``fleet_remote.load_hosts`` non-empty, cached on the hosts.json mtime.

    ``check_action`` runs on every footer refresh, so the file is re-read only
    when it changed (or appeared/disappeared). Never raises."""
    try:
        path = fleet_remote.hosts_path(app.switcher.backup_dir)
        try:
            key: object = path.stat().st_mtime_ns
        except OSError:
            key = None
        cached = getattr(app, "_fleet_hosts_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        configured = bool(fleet_remote.load_hosts(app.switcher.backup_dir))
        app._fleet_hosts_cache = (key, configured)
        return configured
    except Exception:
        return False


def hosts_route_enabled(app: Any) -> bool:
    """The ``h`` binding: on the dashboard (and the auto screen, where the
    in-process leader lives), and only when hosts are configured — a box
    with no fleet shows exactly what it showed before."""
    from claude_swap.tui.autoview import AutoScreen
    from claude_swap.tui.dashboard import DashboardScreen

    try:
        screen = app.screen
    except Exception:
        return False
    if not isinstance(screen, (DashboardScreen, AutoScreen)):
        return False
    return hosts_configured(app)


def open_hosts(app: Any) -> None:
    if isinstance(app.screen, HostsScreen) or not hosts_configured(app):
        return
    app.push_screen(HostsScreen())


# --------------------------------------------------------------------------
# Leader lookup (patched in tests)
# --------------------------------------------------------------------------


def inprocess_leader(app: Any):
    """The FleetLeader of a LIVE engine in this TUI process, or None.

    The auto screen keeps its engine on ``_engine``; the leader is looked up
    on the engine (``fleet_leader`` / ``_fleet_leader``) and then on the
    screen. A dry-run engine never leads (Ruling 4). UI thread only."""
    for screen in reversed(list(getattr(app, "screen_stack", ()))):
        engine = getattr(screen, "_engine", None)
        if engine is None or getattr(engine, "dry_run", True):
            continue
        for owner in (engine, screen):
            for attr in ("fleet_leader", "_fleet_leader"):
                leader = getattr(owner, attr, None)
                if leader is not None and callable(getattr(leader, "kick", None)):
                    return leader
    return None


def leader_snapshot(backup_root, leader=None) -> dict | None:
    """The live leader's snapshot: in-process when we hold it, else through
    ``engine status``'s ``fleet`` block (any engine on this box). Blocking."""
    if leader is not None:
        try:
            snap = leader.snapshot()
        except Exception:
            snap = None
        if isinstance(snap, dict):
            return snap
    try:
        return fleet_remote._leader_snapshot(backup_root)
    except Exception:
        return None


def _no_ssh_runner(argv, **_kwargs):
    """Timer refreshes never ssh: a host the leader has not reported yet
    comes back as an error row instead of a pull."""
    return SimpleNamespace(returncode=255, stdout="", stderr="")


# --------------------------------------------------------------------------
# Pure render helpers
# --------------------------------------------------------------------------


def describe_override(override: dict | None) -> str:
    override = override or {}
    mode = override.get("mode") or "follow"
    if mode == "pin":
        return f"pin {override.get('account')}"
    if mode == "strategy":
        return f"strategy {override.get('strategy')}"
    return "follow"


def _state_label(entry: dict) -> tuple[str, str]:
    """(label, palette role) for the worker's follow state."""
    state = entry.get("state") or "unknown"
    reason = entry.get("reason")
    if state == "following":
        return "following", "sev_ok"
    if state == "overridden":
        return f"overridden: {describe_override(entry.get('override'))}", "accent"
    if state == "autonomous":
        return (f"autonomous ({reason})" if reason else "autonomous"), "sev_warn"
    return (f"{state} ({reason})" if reason else state), "muted"


def target_slot(entry: dict, local_active: str | None) -> str | None:
    """The slot this worker is meant to be on, or None (strategy / unknown).

    A pin override names it (the worker's ``pin N`` reason when it reports
    one, else the saved override); ``following`` means the dev box's active."""
    state = entry.get("state")
    if state == "overridden":
        reason = str(entry.get("reason") or "")
        if reason.startswith("pin "):
            return reason[4:].strip() or None
        override = entry.get("override") or {}
        if override.get("mode") == "pin" and override.get("account") is not None:
            return str(override["account"])
        return None
    if state == "following" and local_active is not None:
        return str(local_active)
    return None


def _off_target(entry: dict, target: str | None) -> bool:
    if target is None or entry.get("state") not in ("following", "overridden"):
        return False
    on_target = entry.get("on_target")
    if isinstance(on_target, bool):
        return not on_target
    # An older worker reports no on_target: judge by the active slot.
    active = entry.get("active")
    return active is not None and str(active) != target


def host_row_text(
    entry: dict, *, leading: bool, palette: Palette, local_active: str | None = None
) -> Text:
    """Two lines per worker: state at a glance, then override/health detail.

    A worker following (or pinned) but not yet on its target shows
    ``active 4 → 2`` (current → target)."""
    text = Text()
    text.append(f"{entry.get('name') or '?':<16}", style=f"bold {palette.foreground}")
    if not entry.get("reachable"):
        text.append("unreachable", style=palette.sev_crit)
        error = entry.get("error")
        if error:
            text.append(f"  {error}", style=palette.muted)
    else:
        label, role = _state_label(entry)
        text.append(f"{label:<28}", style=getattr(palette, role))
        active = entry.get("active")
        text.append(f"  active {active if active is not None else '?'}", style=palette.foreground)
        target = target_slot(entry, local_active)
        if _off_target(entry, target):
            text.append(f" → {target}", style=palette.sev_warn)
        age = entry.get("lease_age_s")
        if isinstance(age, (int, float)):
            text.append(f"  lease {format_duration(max(0.0, age))}", style=palette.muted)
        else:
            text.append("  no lease", style=palette.muted)
    text.append("\n" + " " * 16)
    override = entry.get("override") or {}
    text.append(f"override {describe_override(override)}", style=palette.muted)
    pending = override.get("mode", "follow") != "follow" and entry.get("state") != "overridden"
    if pending and entry.get("reachable"):
        note = "pending next push" if leading else "applies only while this box leads"
        text.append(f" ({note})", style=palette.muted)
    relogin = [str(n) for n in entry.get("relogin") or []]
    if relogin:
        text.append(f"  ·  re-login {', '.join(relogin)}", style=palette.sev_warn)
    drift = entry.get("drift")
    if drift is True:
        text.append("  ·  settings drift", style=palette.sev_warn)
    elif drift is False:
        text.append("  ·  in sync", style=palette.muted)
    else:
        text.append("  ·  drift ?", style=palette.muted)
    return text


def header_text(*, leading: bool, kickable: bool, workers: int, palette: Palette) -> Text:
    text = Text()
    if leading:
        how = "pushed at once" if kickable else "applied at the leader's next push"
        text.append(
            f"This box leads {workers} worker(s): overrides are {how}.",
            style=palette.foreground,
        )
    else:
        text.append(
            "This box is not leading (no live engine: cswap auto, or the auto "
            "screen after l). Workers run their own fallback; overrides are "
            "saved but apply only while this box leads.",
            style=palette.muted,
        )
    return text


def format_push_result(results: list[dict]) -> str:
    lines: list[str] = []
    for res in results or []:
        name = res.get("name") or "?"
        if not res.get("ok"):
            lines.append(f"{name}: push failed: {res.get('error') or 'unknown error'}")
            continue
        changes: list[str] = []
        for ch in res.get("settings") or []:
            changes.append(f"  {ch.get('key')}: {ch.get('from')!r} -> {ch.get('to')!r}")
        for ch in res.get("accounts") or []:
            changes.append(
                f"  account {ch.get('account')} {ch.get('field')}: "
                f"{ch.get('from')!r} -> {ch.get('to')!r}"
            )
        for sk in res.get("skipped") or []:
            changes.append(f"  account {sk.get('account')} skipped ({sk.get('reason')})")
        if changes:
            lines.append(f"{name}: changed")
            lines.extend(changes)
        else:
            lines.append(f"{name}: already in sync, nothing changed")
    return "\n".join(lines) or "no hosts pushed"


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


class HostsScreen(Screen):
    DEFAULT_CSS = """
    HostsScreen #fleet-top {
        height: auto;
        background: $background;
        padding: 1 2;
    }
    HostsScreen #fleet-title-row {
        height: 1;
        margin-bottom: 1;
    }
    HostsScreen #fleet-badge {
        width: auto;
        text-style: bold;
    }
    HostsScreen #fleet-badge.dry {
        background: $panel;
        color: $warning;
    }
    HostsScreen #fleet-badge.live {
        background: $primary;
        color: $background;
    }
    HostsScreen #fleet-source {
        width: 1fr;
        color: $secondary;
        padding-left: 2;
    }
    HostsScreen #fleet-hosts {
        height: 1fr;
        border: none;
        background: $background;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("f", "follow", "Follow"),
        Binding("p", "pin", "Pin"),
        Binding("s", "cycle_strategy", "Strategy"),
        Binding("u", "push", "Push settings"),
        Binding("r", "refresh", "Refresh (ssh)"),
        Binding("escape,q", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._doc: dict | None = None
        self._entries: list[dict] = []
        self._leading = False
        self._kickable = False
        self._loading = False
        self._acting = False
        self._source_note = ""
        self._timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="fleet-top"):
            with Horizontal(id="fleet-title-row"):
                yield Static(" NOT LEADING ", id="fleet-badge", classes="dry")
                yield Static("loading…", id="fleet-source")
            yield Static("", id="fleet-header")
        yield OptionList(id="fleet-hosts")
        yield Footer()

    # -- lifecycle ------------------------------------------------------------

    def on_mount(self) -> None:
        self.query_one("#fleet-hosts", OptionList).focus()
        self._render_header()
        self._load(mode="open")
        self._timer = self.set_interval(LEADER_REFRESH_S, self._on_timer)
        self.watch(self.app, "theme", self._on_theme_change)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _on_theme_change(self, _theme: str) -> None:
        self._render_header()
        self._render_rows()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#fleet-hosts", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#fleet-hosts", OptionList).action_cursor_up()

    # -- data -----------------------------------------------------------------

    def _on_timer(self) -> None:
        self._load(mode="timer")

    def _load(self, *, mode: str) -> None:
        """``open``: leader if leading, else one ssh pull. ``timer``: leader
        only, never ssh. ``pull``: a fresh ssh pull of every host."""
        if self._loading:
            return
        self._loading = True
        leader = inprocess_leader(self.app)
        if mode != "timer":
            self.query_one("#fleet-source", Static).update(
                "pulling over ssh…" if mode == "pull" else "loading…"
            )
        self.run_worker(
            partial(self._load_blocking, mode, leader),
            thread=True,
            group="fleet-status",
            exit_on_error=False,
            name=f"fleet-status-{mode}",
        )

    def _load_blocking(self, mode: str, leader) -> None:
        root = self.app.switcher.backup_dir
        doc: dict | None = None
        error: str | None = None
        try:
            snap = leader_snapshot(root, leader)
            leading = isinstance(snap, dict)
            if mode == "pull" or (mode == "open" and not leading):
                doc = fleet_remote.fleet_status(self.app.switcher, root, leader_snapshot=None)
                note = f"pulled over ssh {time.strftime('%H:%M:%S')}"
            elif leading:
                doc = fleet_remote.fleet_status(
                    self.app.switcher, root, leader_snapshot=snap, runner=_no_ssh_runner
                )
                for entry in doc.get("hosts") or []:
                    if entry.get("source") == "pull":
                        entry["error"] = _NOT_REPORTED
                note = f"from the leader {time.strftime('%H:%M:%S')}"
            else:
                note = None  # timer while not leading: keep the last pull
        except Exception as e:  # never kill the screen on a fleet failure
            leading = False
            note = None
            error = f"fleet status failed ({type(e).__name__})"
        self._post(self._apply_load, doc, leading, leader is not None and leading, note, error)

    def _post(self, fn, *args) -> None:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass  # app shutting down

    def _apply_load(
        self, doc: dict | None, leading: bool, kickable: bool, note: str | None, error: str | None
    ) -> None:
        self._loading = False
        if not self.is_attached:
            return
        self._leading, self._kickable = leading, kickable
        if doc is not None:
            self._doc = doc
            self._entries = [
                e for e in doc.get("hosts") or [] if isinstance(e, dict) and not e.get("local")
            ]
        if note is not None:
            self._source_note = note
        if error is not None:
            self.app.notify(error, title="Hosts", severity="warning")
        self._render_header()
        self._render_rows()

    # -- render ---------------------------------------------------------------

    def _palette(self) -> Palette:
        return Palette.from_theme(self.app.current_theme)

    def _render_header(self) -> None:
        badge = self.query_one("#fleet-badge", Static)
        if self._leading:
            badge.update(" LEADING ")
            badge.set_classes("live")
        else:
            badge.update(" NOT LEADING ")
            badge.set_classes("dry")
        if not self._loading:
            self.query_one("#fleet-source", Static).update(self._source_note)
        self.query_one("#fleet-header", Static).update(
            header_text(
                leading=self._leading,
                kickable=self._kickable,
                workers=len(self._entries),
                palette=self._palette(),
            )
        )

    def _render_rows(self) -> None:
        options = self.query_one("#fleet-hosts", OptionList)
        selected = self._selected_name()
        palette = self._palette()
        options.clear_options()
        if not self._entries:
            options.add_option(
                Option(
                    Text(
                        "no worker hosts (cswap fleet hosts add <ssh-alias>)"
                        if self._doc is not None
                        else "loading…",
                        style=palette.muted,
                    ),
                    disabled=True,
                )
            )
            return
        local = next(
            (h for h in (self._doc or {}).get("hosts") or [] if isinstance(h, dict) and h.get("local")),
            {},
        )
        local_active = local.get("active")
        options.add_options(
            [
                Option(
                    host_row_text(
                        e, leading=self._leading, palette=palette,
                        local_active=str(local_active) if local_active is not None else None,
                    ),
                    id=e["name"],
                )
                for e in self._entries
            ]
        )
        names = [e["name"] for e in self._entries]
        options.highlighted = names.index(selected) if selected in names else 0

    def _selected_name(self) -> str | None:
        options = self.query_one("#fleet-hosts", OptionList)
        idx = options.highlighted
        if idx is None or not self._entries:
            return None
        try:
            return options.get_option_at_index(idx).id
        except Exception:
            return None

    def _selected_entry(self) -> dict | None:
        name = self._selected_name()
        return next((e for e in self._entries if e.get("name") == name), None)

    # -- keys -----------------------------------------------------------------

    def action_refresh(self) -> None:
        leader = inprocess_leader(self.app)
        if leader is not None:
            try:
                leader.kick()
            except Exception:
                pass
        self._load(mode="pull")

    def action_follow(self) -> None:
        entry = self._selected_entry()
        if entry is not None:
            self._set_override(entry["name"], {"mode": "follow"})

    def action_cycle_strategy(self) -> None:
        entry = self._selected_entry()
        if entry is None or not SELECTABLE_STRATEGIES:
            return
        current = entry.get("override") or {}
        if current.get("mode") == "strategy" and current.get("strategy") in SELECTABLE_STRATEGIES:
            idx = SELECTABLE_STRATEGIES.index(current["strategy"]) + 1
        else:
            idx = 0
        strategy = SELECTABLE_STRATEGIES[idx % len(SELECTABLE_STRATEGIES)]
        self._set_override(entry["name"], {"mode": "strategy", "strategy": strategy})

    def _managed_slots(self) -> list[str]:
        snap = self.app.snapshot
        return [acc.number for acc in (snap.accounts if snap else ())]

    def action_pin(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        slots = self._managed_slots()
        if not slots:
            self.app.notify("No managed accounts known yet", title="Pin", severity="warning")
            return
        here = self.app.snapshot.active_number if self.app.snapshot else None
        rows = [
            PickRow(
                number=n,
                email="",
                tags=tuple(
                    t
                    for t, on in (
                        ("active here", n == here),
                        (f"active on {entry['name']}", n == str(entry.get("active"))),
                    )
                    if on
                ),
            )
            for n in slots
        ]
        self.app.push_screen(
            AccountPickModal(rows, title=f"Pin {entry['name']} to account"),
            partial(self._on_pin, entry["name"]),
        )

    def _on_pin(self, name: str, number: str | None) -> None:
        if number is None or not self.is_attached:
            return
        if str(number) not in self._managed_slots():
            self.app.notify(
                f"Account {number} is not a managed slot", title="Pin", severity="error"
            )
            return
        self._set_override(name, {"mode": "pin", "account": str(number)})

    def _set_override(self, name: str, override: dict) -> None:
        if self._acting:
            self.app.notify("Another fleet action is still running", severity="warning")
            return
        self._acting = True
        leader = inprocess_leader(self.app)
        self.run_worker(
            partial(self._override_blocking, name, override, leader, self._leading),
            thread=True,
            group="fleet-action",
            exit_on_error=False,
            name=f"fleet-override-{name}",
        )

    def _override_blocking(self, name: str, override: dict, leader, leading: bool) -> None:
        root = self.app.switcher.backup_dir
        try:
            host = fleet_remote.set_override(root, name, override)
        except Exception as e:
            self._post(self._override_done, name, None, str(e) or type(e).__name__, False)
            return
        kicked = False
        if leader is not None:
            try:
                leader.kick()
                kicked = True
            except Exception:
                kicked = False
        self._post(self._override_done, name, host, None, kicked, leading)

    def _override_done(
        self, name: str, host, error: str | None, kicked: bool, leading: bool = False
    ) -> None:
        self._acting = False
        if error is not None:
            self.app.notify(f"{name}: {error}", title="Override", severity="error")
            return
        override = dict(getattr(host, "override", None) or {})
        for entry in self._entries:
            if entry.get("name") == name:
                entry["override"] = override
        if kicked:
            when = "pushed now"
        elif leading:
            when = "applies at the leader's next push"
        else:
            when = "applies only while this box leads"
        self.app.notify(f"{name}: {describe_override(override)} saved; {when}", title="Override")
        if self.is_attached:
            self._render_rows()

    def action_push(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        name = entry["name"]
        self.app.push_screen(
            ConfirmModal(
                f"Push this box's autoswitch settings and per-account policy to {name}?\n\n"
                "Written on the worker now, identity-checked per slot.",
                title="Push settings",
                yes_label="Push",
            ),
            partial(self._on_push_confirm, name),
        )

    def _on_push_confirm(self, name: str, confirmed: bool | None) -> None:
        if not confirmed:
            return
        if self._acting:
            self.app.notify("Another fleet action is still running", severity="warning")
            return
        self._acting = True
        self.app.notify(f"Pushing settings to {name}…", timeout=3)
        self.run_worker(
            partial(self._push_blocking, name),
            thread=True,
            group="fleet-action",
            exit_on_error=False,
            name=f"fleet-push-{name}",
        )

    def _push_blocking(self, name: str) -> None:
        root = self.app.switcher.backup_dir
        try:
            host = fleet_remote.find_host(fleet_remote.load_hosts(root), name)
            results = fleet_remote.push(self.app.switcher, [host], dry_run=False)
            body = format_push_result(results)
        except Exception as e:
            body = f"{name}: push failed: {str(e) or type(e).__name__}"
        self._post(self._push_done, name, body)

    def _push_done(self, name: str, body: str) -> None:
        self._acting = False
        self.app.push_screen(OutputModal(f"Push settings — {name}", body))
