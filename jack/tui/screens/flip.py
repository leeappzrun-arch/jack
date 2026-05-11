"""Modal: prompts the user to flip the record, then resumes recording."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class FlipModal(ModalScreen[bool]):
    """Modal asking the user to flip to Side B. Returns True on confirm."""

    BINDINGS = [
        Binding("enter", "resume", "Resume", show=True, priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="flip-dialog"):
                yield Static("Side A complete.", classes="flip-headline")
                yield Static("")
                yield Static("Flip the record to Side B, then press Enter.")
                yield Static("")
                yield Button("Resume (Enter)", variant="success", id="resume-btn")

    def action_resume(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "resume-btn":
            self.dismiss(True)
