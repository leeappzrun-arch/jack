"""Modal: prompts the user to flip the record, then resumes recording."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class FlipModal(ModalScreen[bool]):
    """Modal asking the user to flip to the next side. Returns True on confirm."""

    BINDINGS = [
        Binding("enter", "resume", "Resume", show=True, priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, completed_side: str = "A", next_side: str = "B") -> None:
        super().__init__()
        self.completed_side = completed_side
        self.next_side = next_side

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="flip-dialog"):
                yield Static(
                    f"Side {self.completed_side} complete.",
                    classes="flip-headline",
                )
                yield Static("")
                yield Static(
                    f"Flip / change the record to Side {self.next_side}, then press Enter."
                )
                yield Static("")
                yield Button("Resume (Enter)", variant="success", id="resume-btn")

    def action_resume(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "resume-btn":
            self.dismiss(True)
