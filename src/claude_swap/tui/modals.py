"""Modal screens: confirmations, token entry, and captured-output display."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from claude_swap.tui.theme import Palette


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation. Dismisses with True only on explicit confirm.

    Keyboard-first: ←/→ move between the buttons (Enter presses the focused
    one), y/n answer directly, Esc cancels. Clicking still works.
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape", "cancel", "No", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self, message: str, *, title: str = "Confirm", yes_label: str = "Yes"
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._yes_label = yes_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._message, classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button(self._yes_label, id="yes")
                yield Button("Cancel", id="no")
            yield Static(
                f"← → · enter  ·  y {self._yes_label.lower()}  ·  n / esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class TokenForm:
    """What the add-token modal collects."""

    token: str
    email: str | None
    slot: int | None


class AddTokenModal(ModalScreen["TokenForm | None"]):
    """Collects a setup-token/API key, optional email label, optional slot.

    ←/→ only reach the screen when a Button is focused (a focused Input
    consumes them for cursor movement), so they safely double as button
    navigation.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Add account from token", classes="modal-title")
            yield Static(
                "OAuth setup-token (sk-ant-oat…) or managed API key "
                "(sk-ant-api…); the type is auto-detected.",
                classes="modal-body",
            )
            yield Input(password=True, placeholder="token (required)", id="token")
            yield Input(placeholder="email label (optional)", id="email")
            yield Input(placeholder="slot number (optional)", id="slot", type="integer")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Add", id="add")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter add  ·  tab next field  ·  esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        email = self.query_one("#email", Input).value.strip() or None
        slot_raw = self.query_one("#slot", Input).value.strip()
        if not token:
            self.query_one("#form-error", Static).update("Token is required.")
            return
        slot: int | None = None
        if slot_raw:
            try:
                slot = int(slot_raw)
            except ValueError:
                self.query_one("#form-error", Static).update(
                    "Slot must be a number."
                )
                return
            if slot < 1:
                self.query_one("#form-error", Static).update("Slot must be >= 1.")
                return
        self.dismiss(TokenForm(token=token, email=email, slot=slot))

    def action_cancel(self) -> None:
        self.dismiss(None)


class OutputModal(ModalScreen[None]):
    """Scrollable display of captured (ANSI-colored) action output."""

    BINDINGS = [Binding("escape,q,enter", "dismiss_modal", "Close", show=False)]

    def __init__(self, title: str, output: str) -> None:
        super().__init__()
        self._title = title
        self._output = output

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-output"):
                yield Static(Text.from_ansi(self._output.rstrip() or "(no output)"))
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", id="close")
            yield Static("esc close", classes="modal-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class PickRow:
    """One account row for :class:`AccountPickModal` (plain data, no engine).

    ``pct`` is the binding pct on the engine's own windows (None = unknown);
    ``label`` replaces it (sentinel wording / "usage unknown"); ``tags`` are
    extra muted markers ("over threshold", "disabled", ...). An ``active``
    row is shown but cannot be picked."""

    number: str
    email: str
    pct: float | None = None
    label: str | None = None
    active: bool = False
    tags: tuple[str, ...] = ()


class AccountPickModal(ModalScreen["str | None"]):
    """Manual pick (`m` on the auto screen): choose the account to switch to.

    Dismisses with the picked account number, or None on escape. ↑/↓ +
    enter, or a digit 1-9 picks that account number directly. The active
    row is disabled; every other row is selectable — a refusal (over the
    threshold, out of rotation) is the engine's to explain in the log."""

    DEFAULT_CSS = """
    AccountPickModal {
        align: center middle;
        background: $background 60%;
    }
    AccountPickModal OptionList {
        height: auto;
        max-height: 16;
        border: none;
        background: $surface;
        padding: 0;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        *(
            Binding(str(d), f"pick_number('{d}')", show=False)
            for d in range(1, 10)
        ),
    ]

    def __init__(self, rows: list[PickRow], *, title: str = "Manual pick") -> None:
        super().__init__()
        self._rows = list(rows)
        self._title = title

    def compose(self) -> ComposeResult:
        palette = Palette.from_theme(self.app.current_theme)
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield OptionList(
                *(
                    Option(self._row_text(r, palette), id=r.number, disabled=r.active)
                    for r in self._rows
                ),
                id="pick-list",
            )
            yield Static(
                "↑ ↓ · enter pick  ·  1-9 account  ·  esc cancel",
                classes="modal-hint",
            )

    @staticmethod
    def _row_text(row: PickRow, palette: Palette) -> Text:
        text = Text()
        text.append(f"{row.number:>2}  ", style=palette.foreground)
        text.append(row.email, style=palette.foreground)
        if row.label is not None:
            text.append(f"  {row.label}", style=palette.muted)
        elif row.pct is not None:
            text.append(f"  {row.pct:3.0f}% used", style=palette.severity(row.pct))
        for tag in (("active",) if row.active else ()) + row.tags:
            text.append(f"  {tag}", style=palette.muted)
        return text

    def on_mount(self) -> None:
        options = self.query_one("#pick-list", OptionList)
        first = next(
            (i for i, r in enumerate(self._rows) if not r.active), None
        )
        if first is not None:
            options.highlighted = first
        options.focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        self._pick(event.option.id)

    def action_pick_number(self, number: str) -> None:
        self._pick(number)

    def _pick(self, number: str | None) -> None:
        row = next((r for r in self._rows if r.number == number), None)
        if row is None or row.active:
            return
        self.dismiss(row.number)

    def action_cancel(self) -> None:
        self.dismiss(None)
