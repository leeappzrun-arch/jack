"""Main menu: ASCII logo + Test / Rip entry points."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Middle, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Footer, Static

_LOGO = r"""
     ██╗  █████╗   ██████╗ ██╗  ██╗
     ██║ ██╔══██╗ ██╔════╝ ██║ ██╔╝
     ██║ ███████║ ██║      █████╔╝
██╗  ██║ ██╔══██║ ██║      ██╔═██╗
╚█████╔╝ ██║  ██║ ╚██████╗ ██║  ██╗
 ╚════╝  ╚═╝  ╚═╝  ╚═════╝ ╚═╝  ╚═╝
"""


class MainMenuScreen(Screen):
    """Landing page. Pick Test (level check) or Rip (full workflow)."""

    BINDINGS = [
        Binding("t", "go_test", "Test"),
        Binding("r", "go_rip", "Rip"),
        Binding("q", "quit", "Quit"),
    ]

    class StartTest(Message):
        pass

    class StartRip(Message):
        pass

    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                yield Static(_LOGO, id="menu-logo")
            with Center():
                yield Static("Vinyl ripper", id="menu-tagline")
            with Center():
                with Vertical(id="menu-buttons"):
                    yield Button("Test input  (t)", id="menu-test", variant="primary")
                    yield Button("Rip a record  (r)", id="menu-rip", variant="success")
        yield Footer()

    def action_go_test(self) -> None:
        self.post_message(self.StartTest())

    def action_go_rip(self) -> None:
        self.post_message(self.StartRip())

    @on(Button.Pressed, "#menu-test")
    def _on_test(self) -> None:
        self.post_message(self.StartTest())

    @on(Button.Pressed, "#menu-rip")
    def _on_rip(self) -> None:
        self.post_message(self.StartRip())
