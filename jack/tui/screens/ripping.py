"""Ripping screen — live VU, tracklist, artwork, progress.

UI orchestration:

- A `RipController` runs in a worker thread (see `jack.audio.recorder`).
- Audio data never touches the UI thread: the controller emits
  `ControllerEvent`s via the callback we pass in, and our handler funnels
  each event through `app.call_from_thread` to mutate widgets safely.
- A 10 Hz UI-side `set_interval` poll reads the latest RMS/peak from the
  controller and re-renders the VU meter — cheaper than posting an event
  per audio block.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, ProgressBar, Static

from jack.audio.recorder import ControllerEvent, EventKind, RipController
from jack.metadata.artwork import (
    Artwork,
    chafa_available,
    fallback_art,
    render_with_chafa,
)
from jack.state import TrackStatus
from jack.tui.screens.complete import CompletionScreen
from jack.tui.screens.flip import FlipModal
from jack.tui.widgets.vu import VUMeter

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


# ---------------------------------------------------------------------------
# Ripping screen
# ---------------------------------------------------------------------------


class RippingScreen(Screen):
    """Live rip view."""

    BINDINGS = [
        Binding("p", "toggle_pause", "Pause / Resume"),
        Binding("f", "force_flip", "Flip Record"),
        Binding("q", "quit_rip", "Stop & Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._controller: RipController | None = None
        self._vu_timer = None
        self._track_started_at: float | None = None
        self._transitioned = False  # guard against double-pushing CompletionScreen

    # ---- compose --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="rip-body"):
            with Vertical(id="rip-left"):
                yield Static("", id="artwork", markup=False)
                yield Static("", id="album-info")
            with Vertical(id="rip-right"):
                yield Static("", id="side-label")
                tracks_table = DataTable(id="tracks", cursor_type="row", zebra_stripes=True)
                tracks_table.show_cursor = False
                yield tracks_table
        yield VUMeter(id="vu")
        with Horizontal(id="progress-row"):
            with Vertical(classes="progress-col"):
                yield Static("Track", classes="progress-label", id="track-progress-label")
                yield ProgressBar(total=100, show_eta=False, show_percentage=True, id="track-progress")
            with Vertical(classes="progress-col"):
                yield Static("Overall", classes="progress-label")
                yield ProgressBar(total=100, show_eta=False, show_percentage=True, id="overall-progress")
        yield Static("", id="rip-status")
        yield Footer()

    # ---- mount ----------------------------------------------------------

    def on_mount(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]

        # Album header info
        self.query_one("#album-info", Static).update(
            f"[b]{app.state.artist}[/b]\n{app.state.album}\n{app.state.date or ''}"
        )
        self._update_side_label()

        # Tracklist
        tbl = self.query_one("#tracks", DataTable)
        tbl.add_columns("  ", "  #", "Title", "Expected", "Actual")
        for t in app.state.tracks:
            tbl.add_row(
                STATUS_ICON[t.status],
                f"{t.number:>2}",
                t.title,
                _fmt_dur(t.duration_ms),
                _fmt_dur(t.actual_duration_ms),
            )

        # Artwork
        self._render_artwork()

        # Progress bars
        self.query_one("#overall-progress", ProgressBar).update(
            total=max(1, len(app.state.tracks)), progress=0
        )

        # Controller
        self._controller = RipController(
            state=app.state,
            device_index=app.config.device_index,
            sample_rate=app.config.sample_rate,
            channels=app.config.channels,
            monitor_device_index=app.config.monitor_device_index,
            threshold_db=app.config.silence_threshold_db,
            silence_duration_s=app.config.silence_duration_s,
            min_track_duration_s=app.config.min_track_duration_s,
            min_track_fraction_of_expected=app.config.min_track_fraction_of_expected,
            output_dir=Path(app.config.output_dir),
            artwork=app.artwork,
            on_event=self._on_controller_event,
        )
        try:
            self._controller.start()
        except Exception as e:
            logger.exception("controller start failed")
            self.query_one("#rip-status", Static).update(
                f"[red]Failed to start capture: {e}[/]"
            )
            return

        # 10 Hz VU + track-progress refresh
        self._vu_timer = self.set_interval(0.1, self._tick_ui)
        self.query_one("#rip-status", Static).update(
            f"Listening at {self._controller.sample_rate} Hz. Drop the needle and press play."
        )

    # ---- shutdown -------------------------------------------------------

    def on_unmount(self) -> None:
        if self._vu_timer is not None:
            self._vu_timer.stop()
        if self._controller is not None:
            try:
                self._controller.stop()
            except Exception:
                logger.exception("controller.stop failed during unmount")

    # ---- artwork --------------------------------------------------------

    def _render_artwork(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        widget = self.query_one("#artwork", Static)
        if app.artwork is not None and chafa_available():
            try:
                # chafa emits raw ANSI escapes; Text.from_ansi parses them into
                # Rich style spans so they render as colors, not visible text.
                ansi = render_with_chafa(app.artwork.image_path, width=24, height=12)
                widget.update(Text.from_ansi(ansi))
                return
            except Exception:
                logger.exception("chafa render failed")
        widget.update(fallback_art())

    def _update_side_label(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        self.query_one("#side-label", Static).update(
            f"[b]SIDE {app.state.side.value}[/b]"
        )

    # ---- 10 Hz UI tick --------------------------------------------------

    def _tick_ui(self) -> None:
        if self._controller is None:
            return
        rms, peak = self._controller.vu_levels()
        self.query_one("#vu", VUMeter).set_levels(rms, peak)

        # Track progress based on elapsed actual frames vs expected ms.
        app: "JackApp" = self.app  # type: ignore[assignment]
        track = app.state.current_track()
        track_bar = self.query_one("#track-progress", ProgressBar)
        track_label = self.query_one("#track-progress-label", Static)
        if track is not None and track.status == TrackStatus.RECORDING and track.duration_ms:
            # The writer's frame count is the source of truth for actual elapsed
            # length, but we don't have a stable handle to it here. Use a wall-
            # clock approximation seeded at TRACK_STARTED instead.
            import time
            elapsed_ms = int((time.monotonic() - (self._track_started_at or time.monotonic())) * 1000)
            pct = min(100, int(elapsed_ms / track.duration_ms * 100))
            track_bar.update(total=100, progress=pct)
            track_label.update(
                f"Track  {_fmt_dur(elapsed_ms).strip()} / {_fmt_dur(track.duration_ms).strip()}"
            )
        elif track is not None and track.status == TrackStatus.RECORDING:
            # Recording but MB had no expected duration — show elapsed only.
            import time
            elapsed_ms = int((time.monotonic() - (self._track_started_at or time.monotonic())) * 1000)
            track_label.update(f"Track  {_fmt_dur(elapsed_ms).strip()}")
        elif track is None or track.status == TrackStatus.WAITING:
            track_bar.update(total=100, progress=0)
            track_label.update("Track")

    # ---- controller event bridge ----------------------------------------

    def _on_controller_event(self, ev: ControllerEvent) -> None:
        """May run on worker or UI thread (start/pause emit synchronously)."""
        try:
            self.app.call_from_thread(self._handle_event, ev)
        except RuntimeError:
            # Already on the UI thread — invoke directly.
            self._handle_event(ev)

    def _handle_event(self, ev: ControllerEvent) -> None:
        # Late events can land after the screen starts unmounting (encoder
        # pool finishing). Bail rather than NoMatches the widgets.
        if not self.is_attached:
            return
        if ev.kind == EventKind.READY:
            self._set_status("Listening...")
        elif ev.kind == EventKind.TRACK_STARTED:
            import time
            self._track_started_at = time.monotonic()
            self._refresh_track_row(ev.track_index)
            self._set_status(f"Recording track {ev.track_index + 1}")
        elif ev.kind == EventKind.TRACK_FINISHED:
            self._refresh_track_row(ev.track_index)
            self._bump_overall_progress()
            if ev.message:
                self._set_status(f"[yellow]Track {ev.track_index + 1} error: {ev.message}[/]")
            else:
                self._set_status(
                    f"Wrote track {ev.track_index + 1} ({_fmt_dur(ev.actual_duration_ms)})"
                )
            self._maybe_transition_to_completion()
        elif ev.kind == EventKind.SIDE_COMPLETE:
            completed = ev.completed_side.value if ev.completed_side else "?"
            next_side = ev.next_side.value if ev.next_side else "?"
            self._set_status(
                f"Side {completed} complete — pausing to change to Side {next_side}."
            )
            self._begin_flip_flow(
                completed_side=completed,
                next_side=next_side,
            )
        elif ev.kind == EventKind.PAUSED:
            self._set_status("Paused.")
        elif ev.kind == EventKind.RESUMED:
            self._update_side_label()
            self._set_status("Resumed.")
        elif ev.kind == EventKind.STOPPED:
            self._set_status("Stopped.")
            # Step 9 will replace this with a transition to the completion screen.
        elif ev.kind == EventKind.ERROR:
            self._set_status(f"[red]Error: {ev.message}[/]")

    # ---- helpers --------------------------------------------------------

    def _refresh_track_row(self, idx: int | None) -> None:
        if idx is None:
            return
        app: "JackApp" = self.app  # type: ignore[assignment]
        if not (0 <= idx < len(app.state.tracks)):
            return
        t = app.state.tracks[idx]
        tbl = self.query_one("#tracks", DataTable)
        # Update individual cells. DataTable rows are keyed by position 0..N-1.
        try:
            tbl.update_cell_at((idx, 0), STATUS_ICON[t.status])
            tbl.update_cell_at((idx, 4), _fmt_dur(t.actual_duration_ms))
        except Exception:
            logger.exception("failed to update track row %d", idx)

    def _bump_overall_progress(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        done = sum(1 for t in app.state.tracks if t.status in (TrackStatus.DONE, TrackStatus.WARNING))
        bar = self.query_one("#overall-progress", ProgressBar)
        bar.update(total=max(1, len(app.state.tracks)), progress=done)

    def _set_status(self, text: str) -> None:
        self.query_one("#rip-status", Static).update(text)

    def _maybe_transition_to_completion(self) -> None:
        """If every track is in a terminal state, switch to the completion screen.

        Controller shutdown joins worker + encoder pool. Running it on the UI
        thread inside a `call_from_thread` callback deadlocks (the encoder
        pool may still want to deliver events back to us). Defer to a thread.
        """
        if self._transitioned:
            return
        app: "JackApp" = self.app  # type: ignore[assignment]
        terminal = {TrackStatus.DONE, TrackStatus.WARNING}
        if not app.state.tracks:
            return
        if any(t.status not in terminal for t in app.state.tracks):
            return
        self._transitioned = True
        if self._vu_timer is not None:
            self._vu_timer.stop()
        threading.Thread(
            target=self._shutdown_and_switch,
            name="jack-transition",
            daemon=True,
        ).start()

    def _shutdown_and_switch(self) -> None:
        """Worker-thread helper: drain controller, then switch screens on UI thread."""
        controller = self._controller
        self._controller = None  # so on_unmount skips a second stop()
        if controller is not None:
            try:
                controller.stop()
            except Exception:
                logger.exception("controller.stop failed during transition")
        try:
            self.app.call_from_thread(self.app.switch_screen, CompletionScreen())
        except RuntimeError:
            # App is shutting down — nothing to switch to.
            pass

    # ---- flip flow ------------------------------------------------------

    def _begin_flip_flow(
        self,
        completed_side: str | None = None,
        next_side: str | None = None,
    ) -> None:
        if self._controller is None:
            return
        self._controller.pause()

        # Derive sides from state if the caller didn't supply them (manual flip).
        app: "JackApp" = self.app  # type: ignore[assignment]
        if completed_side is None:
            completed_side = app.state.side.value
        if next_side is None:
            idx = app.state.current_track_index
            if 0 <= idx < len(app.state.tracks):
                next_side = app.state.tracks[idx].side.value
            else:
                next_side = completed_side

        def _after(resumed: bool) -> None:
            if not resumed:
                return
            if self._controller is None:
                return
            self._controller.resume_next_side()
            self._update_side_label()

        self.app.push_screen(
            FlipModal(completed_side=completed_side, next_side=next_side),
            _after,
        )

    # ---- actions --------------------------------------------------------

    def action_toggle_pause(self) -> None:
        if self._controller is None:
            return
        # When awaiting a side change, the FlipModal owns the pause/resume.
        # Detect that as: the controller is paused AND the modal is up — i.e.
        # state.side hasn't yet been advanced to the next track's side.
        app: "JackApp" = self.app  # type: ignore[assignment]
        idx = app.state.current_track_index
        if (
            self._controller._paused.is_set()
            and 0 <= idx < len(app.state.tracks)
            and app.state.tracks[idx].side != app.state.side
        ):
            return
        # Simple toggle — internally setting/clearing the paused event.
        if self._controller._paused.is_set():
            self._controller._paused.clear()
            self._set_status("Resumed.")
        else:
            self._controller.pause()

    def action_force_flip(self) -> None:
        """Manual side flip (used when MB had no side info)."""
        self._begin_flip_flow()

    def action_quit_rip(self) -> None:
        """Stop the rip and show the completion summary with whatever was done."""
        if self._transitioned:
            return
        self._transitioned = True
        if self._vu_timer is not None:
            self._vu_timer.stop()
        threading.Thread(
            target=self._shutdown_and_switch,
            name="jack-quit",
            daemon=True,
        ).start()
