"""Completion screen: shows ripped files, sizes, warnings; offers next steps."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Static

from jack.metadata.artwork import chafa_available, fallback_art, render_with_chafa
from jack.state import AppState, TrackStatus
from jack.tui.screens.setup import SetupScreen

if TYPE_CHECKING:
    from jack.tui.app import JackApp

logger = logging.getLogger(__name__)

STATUS_ICON = {
    TrackStatus.WAITING:   "·",
    TrackStatus.RECORDING: "●",
    TrackStatus.ENCODING:  "⚙",
    TrackStatus.DONE:      "✓",
    TrackStatus.WARNING:   "!",
}


def _fmt_dur(ms: int | None) -> str:
    if not ms:
        return "  --"
    s = ms // 1000
    return f"{s // 60:>2}:{s % 60:02d}"


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "   --"
    if n < 1024:
        return f"{n:>5} B"
    if n < 1024 * 1024:
        return f"{n / 1024:>5.1f} KB"
    return f"{n / (1024 * 1024):>5.1f} MB"


class CompletionScreen(Screen):
    """Post-rip summary."""

    BINDINGS = [
        Binding("o", "open_folder", "Open folder"),
        Binding("r", "rip_another", "Rip another"),
        Binding("q", "quit_app", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="complete-body"):
            with Vertical(id="complete-left"):
                yield Static("", id="complete-artwork", markup=False)
            with Vertical(id="complete-right"):
                yield Static("", id="complete-summary")
                yield DataTable(id="complete-tracks", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="complete-buttons"):
            yield Button("Open folder", id="open", variant="primary")
            yield Button("Rip another", id="another", variant="success")
            yield Button("Quit", id="quit", variant="default")
        yield Footer()

    def on_mount(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        state = app.state

        # Artwork (consistent with ripping screen)
        art = self.query_one("#complete-artwork", Static)
        if app.artwork is not None and chafa_available():
            try:
                ansi = render_with_chafa(app.artwork.image_path, width=24, height=12)
                art.update(Text.from_ansi(ansi))
            except Exception:
                logger.exception("chafa render failed in completion screen")
                art.update(fallback_art())
        else:
            art.update(fallback_art())

        # Summary header
        self.query_one("#complete-summary", Static).update(self._summary_markup(state))

        # File table
        tbl = self.query_one("#complete-tracks", DataTable)
        tbl.add_columns("  ", "  #", "Title", "Exp.", "Actual", "Size", "Notes")
        for t in state.tracks:
            size = None
            if t.output_path is not None:
                try:
                    size = t.output_path.stat().st_size
                except OSError:
                    size = None
            tbl.add_row(
                STATUS_ICON[t.status],
                f"{t.number:>2}",
                t.title,
                _fmt_dur(t.duration_ms),
                _fmt_dur(t.actual_duration_ms),
                _fmt_bytes(size),
                t.warning or "",
            )

    def _summary_markup(self, state: AppState) -> str:
        done = sum(1 for t in state.tracks if t.status == TrackStatus.DONE)
        warn = sum(1 for t in state.tracks if t.status == TrackStatus.WARNING)
        skipped = sum(
            1 for t in state.tracks
            if t.status in (TrackStatus.WAITING, TrackStatus.RECORDING, TrackStatus.ENCODING)
        )
        total_size = 0
        for t in state.tracks:
            if t.output_path is not None:
                try:
                    total_size += t.output_path.stat().st_size
                except OSError:
                    pass
        size_label = _fmt_bytes(total_size).strip()
        lines = [
            f"[b]{state.artist}[/b] — {state.album}",
            (state.date or "") + (f"  •  {state.mbid}" if state.mbid else ""),
            "",
            f"[green]{done} ripped[/]"
            + (f"  •  [yellow]{warn} warning[/]" if warn else "")
            + (f"  •  [red]{skipped} skipped[/]" if skipped else "")
            + f"  •  total {size_label}",
        ]
        return "\n".join(lines)

    # ---- actions --------------------------------------------------------

    def _output_folder(self) -> Path | None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        for t in app.state.tracks:
            if t.output_path is not None:
                return t.output_path.parent
        return Path(app.config.output_dir).expanduser()

    @on(Button.Pressed, "#open")
    def _btn_open(self) -> None:
        self.action_open_folder()

    @on(Button.Pressed, "#another")
    def _btn_another(self) -> None:
        self.action_rip_another()

    @on(Button.Pressed, "#quit")
    def _btn_quit(self) -> None:
        self.action_quit_app()

    def action_open_folder(self) -> None:
        folder = self._output_folder()
        if folder is None or not folder.exists():
            return
        try:
            subprocess.Popen(
                ["xdg-open", str(folder)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("xdg-open not available; cannot open %s", folder)
        except OSError:
            logger.exception("failed to open folder %s", folder)

    def action_rip_another(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        # Fresh state for the next album, but keep config (device, threshold, output dir).
        app.state = AppState()
        app.artwork = None
        app.switch_screen(SetupScreen())

    def action_quit_app(self) -> None:
        self.app.exit()
