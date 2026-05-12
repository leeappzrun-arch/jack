"""Top-level Textual app. Hosts the setup → ripping → complete screens.

The app holds the shared `Config` and `AppState`. Screens read/write via
`self.app.config` and `self.app.state`. Worker threads push to the UI with
`self.app.call_from_thread(...)`.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from textual.app import App

from jack.config import Config
from jack.state import AppState
from jack.tui.screens.menu import MainMenuScreen
from jack.tui.screens.ripping import RippingScreen
from jack.tui.screens.setup import SetupScreen
from jack.tui.screens.test import TestScreen

logger = logging.getLogger(__name__)


class JackApp(App):
    """Vinyl ripper TUI."""

    TITLE = "Jack — Vinyl Ripper"
    CSS_PATH = Path(__file__).parent / "app.tcss"

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config | None = None, state: AppState | None = None) -> None:
        super().__init__()
        self.config = config or Config.load()
        self.state = state or AppState()
        # Set by SetupScreen when artwork is fetched, used later for tagging.
        self.artwork = None
        # When JACK_USE_FIXTURE=1, the MB module is short-circuited with
        # local fixtures. Lets us exercise the UI when MB is down for
        # maintenance.
        self.use_fixture = os.environ.get("JACK_USE_FIXTURE") == "1"

    def on_mount(self) -> None:
        self.push_screen(MainMenuScreen())

    def on_main_menu_screen_start_test(self, _message: MainMenuScreen.StartTest) -> None:
        self.push_screen(TestScreen())

    def on_main_menu_screen_start_rip(self, _message: MainMenuScreen.StartRip) -> None:
        self.push_screen(SetupScreen())

    def on_test_screen_back(self, _message: TestScreen.Back) -> None:
        self.pop_screen()

    def on_setup_screen_begin_ripping(self, _message: SetupScreen.BeginRipping) -> None:
        self.push_screen(RippingScreen())
