"""Setup screen: artist/album → MB search → pick release → device → output dir → Begin.

Long-running work (MB search, release detail fetch, artwork download) runs
on Textual worker threads via `@work(thread=True)`. Worker results land back
on the UI via `app.call_from_thread`.

If MB is unreachable (maintenance, no network), the screen still runs — the
search just surfaces the error in the status line. Setting `JACK_USE_FIXTURE=1`
short-circuits MB calls with bundled JSON fixtures so the UI can be exercised.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from jack.audio.capture import list_input_devices
from jack.metadata import artwork as artwork_mod
from jack.metadata import musicbrainz as mbz
from jack.state import RipPhase

if TYPE_CHECKING:
    from jack.tui.app import JackApp

logger = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures"


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "?"
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


class SetupScreen(Screen):
    """Pre-rip configuration."""

    BINDINGS = [
        ("ctrl+s", "search", "Search"),
        ("ctrl+r", "begin", "Begin Ripping"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._candidates: list[mbz.ReleaseCandidate] = []
        self._details: mbz.ReleaseDetails | None = None

    # ---- compose ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="setup-grid"):
            with Horizontal(classes="row"):
                yield Static("Artist:", classes="label")
                yield Input(placeholder="e.g. Pink Floyd", id="artist")
            with Horizontal(classes="row"):
                yield Static("Album:", classes="label")
                yield Input(placeholder="e.g. The Dark Side of the Moon", id="album")
            yield Button("Search MusicBrainz", id="search", variant="primary")
            yield Static("", id="status")
            table = DataTable(id="results", cursor_type="row", zebra_stripes=True)
            yield table
            with Horizontal(classes="row"):
                yield Static("Device:", classes="label")
                yield Select(options=[], prompt="Detecting devices...", id="device")
            with Horizontal(classes="row"):
                yield Static("Output:", classes="label")
                yield Input(id="output")
            with Horizontal(classes="row"):
                yield Static("Threshold:", classes="label")
                yield Input(
                    id="threshold",
                    placeholder="dB, e.g. -45",
                    tooltip="Silence-detection threshold in dBFS. Lower = ignores more noise.",
                )
            yield Button("Begin Ripping", id="begin", variant="success", disabled=True)
        yield Footer()

    # ---- mount setup -----------------------------------------------------

    def on_mount(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]

        # Output dir + threshold inputs → seed from config
        self.query_one("#output", Input).value = app.config.output_dir
        self.query_one("#threshold", Input).value = f"{app.config.silence_threshold_db:g}"

        # Device dropdown → populate from sounddevice
        select = self.query_one("#device", Select)
        try:
            devices = list_input_devices()
        except Exception as e:
            logger.exception("device enumeration failed")
            select.prompt = f"device error: {e}"
            devices = []
        if devices:
            options = [
                (f"[{d.index}] {d.name}  ({d.max_input_channels}ch, {d.default_samplerate} Hz)",
                 d.index)
                for d in devices
            ]
            select.set_options(options)
            select.prompt = "Select input device"
            # Prefer USB device, or last-used from config
            preferred = app.config.device_index
            if preferred is None:
                for d in devices:
                    if d.is_usb:
                        preferred = d.index
                        break
            if preferred is not None and preferred in {d.index for d in devices}:
                select.value = preferred
        else:
            select.prompt = "No input devices found"

        # DataTable columns
        table = self.query_one("#results", DataTable)
        table.add_columns("Score", "Date", "Country", "Format", "Tracks", "Title")

        if app.use_fixture:
            self._set_status("[fixture mode] MB calls use bundled JSON.")

    # ---- helpers ---------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _populate_results(self, candidates: list[mbz.ReleaseCandidate]) -> None:
        self._candidates = candidates
        table = self.query_one("#results", DataTable)
        table.clear()
        for c in candidates:
            table.add_row(
                str(c.score),
                c.date or "?",
                c.country or "?",
                c.format or "?",
                str(c.track_count),
                f"{c.artist} — {c.title}",
            )
        if candidates:
            self._set_status(
                f"{len(candidates)} result(s). Select a row to load the tracklist."
            )
        else:
            self._set_status("No results.")

    def _can_begin(self) -> bool:
        return (
            self._details is not None
            and self.query_one("#device", Select).value is not Select.BLANK
            and bool(self.query_one("#output", Input).value.strip())
        )

    def _refresh_begin(self) -> None:
        self.query_one("#begin", Button).disabled = not self._can_begin()

    # ---- actions ---------------------------------------------------------

    def action_search(self) -> None:
        self._do_search()

    def action_begin(self) -> None:
        if self._can_begin():
            self._do_begin()

    @on(Button.Pressed, "#search")
    def _on_search(self) -> None:
        self._do_search()

    @on(Button.Pressed, "#begin")
    def _on_begin(self) -> None:
        self._do_begin()

    @on(Input.Submitted, "#artist")
    @on(Input.Submitted, "#album")
    def _on_query_submitted(self) -> None:
        self._do_search()

    @on(Input.Changed, "#output")
    def _on_output_changed(self) -> None:
        self._refresh_begin()

    @on(Select.Changed, "#device")
    def _on_device_changed(self) -> None:
        self._refresh_begin()

    # ---- MusicBrainz search ---------------------------------------------

    def _do_search(self) -> None:
        artist = self.query_one("#artist", Input).value.strip()
        album = self.query_one("#album", Input).value.strip()
        if not artist and not album:
            self._set_status("Enter an artist or album to search.")
            return
        self._set_status(f"Searching MusicBrainz for “{artist} — {album}”...")
        self._search_worker(artist, album)

    @work(thread=True, exclusive=True, group="search")
    def _search_worker(self, artist: str, album: str) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        try:
            if app.use_fixture:
                result = json.loads((FIXTURE_DIR / "mb_search_pink_floyd.json").read_text())
                candidates = mbz.parse_search_results(result, limit=5)
            else:
                mbz.configure(app.config.musicbrainz_contact)
                candidates = mbz.search_releases(artist, album, limit=8)
            self.app.call_from_thread(self._populate_results, candidates)
        except Exception as e:
            logger.exception("MB search failed")
            self.app.call_from_thread(
                self._set_status, f"MusicBrainz search failed: {e}"
            )

    # ---- release selection → fetch details + artwork --------------------

    @on(DataTable.RowSelected, "#results")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if not (0 <= idx < len(self._candidates)):
            return
        chosen = self._candidates[idx]
        self._set_status(f"Loading tracklist for {chosen.title} ({chosen.date})...")
        self._fetch_release_worker(chosen.mbid)

    @work(thread=True, exclusive=True, group="release")
    def _fetch_release_worker(self, mbid: str) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        try:
            if app.use_fixture:
                result = json.loads((FIXTURE_DIR / "mb_release_dsotm_vinyl.json").read_text())
                details = mbz.parse_release(result)
            else:
                mbz.configure(app.config.musicbrainz_contact)
                details = mbz.get_release(mbid)
            self.app.call_from_thread(self._on_release_loaded, details)
        except Exception as e:
            logger.exception("MB release fetch failed")
            self.app.call_from_thread(
                self._set_status, f"Could not load release: {e}"
            )

    def _on_release_loaded(self, details: mbz.ReleaseDetails) -> None:
        self._details = details
        sides = "/".join(details.sides) if details.sides else "(no side info — will prompt)"
        side_a = details.side_a_count if details.side_a_count else "?"
        self._set_status(
            f"Loaded {len(details.tracks)} tracks  •  sides: {sides}  •  side A: {side_a}"
            "  •  Fetching artwork…"
        )
        self._refresh_begin()
        self._fetch_artwork_worker(details.mbid)

    @work(thread=True, exclusive=True, group="artwork")
    def _fetch_artwork_worker(self, mbid: str) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        if app.use_fixture:
            self.app.call_from_thread(
                self._set_status,
                "Loaded (fixture mode — skipping artwork fetch).",
            )
            return
        try:
            art = artwork_mod.fetch_front_cover(mbid)
            self.app.call_from_thread(self._on_artwork_loaded, art)
        except Exception as e:
            logger.exception("artwork fetch failed")
            self.app.call_from_thread(
                self._set_status, f"Tracklist loaded; artwork unavailable: {e}"
            )

    def _on_artwork_loaded(self, art) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]
        app.artwork = art
        if art is None:
            self._set_status("Tracklist loaded. (No cover art on CAA for this release.)")
        else:
            dims = f" {art.width}×{art.height}" if art.width else ""
            self._set_status(f"Tracklist + artwork loaded{dims}. Ready to rip.")

    # ---- Begin ----------------------------------------------------------

    def _do_begin(self) -> None:
        """Push selections into AppState and transition to the ripping screen."""
        if self._details is None:
            self._set_status("Pick a release first.")
            return
        app: "JackApp" = self.app  # type: ignore[assignment]

        device_value = self.query_one("#device", Select).value
        if device_value is Select.BLANK:
            self._set_status("Pick an input device first.")
            return

        output_value = self.query_one("#output", Input).value.strip()
        if not output_value:
            self._set_status("Set an output directory.")
            return
        output_path = Path(output_value).expanduser()
        try:
            output_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._set_status(f"Cannot create output dir: {e}")
            return

        threshold_value = self.query_one("#threshold", Input).value.strip()
        try:
            threshold_db = float(threshold_value)
        except ValueError:
            self._set_status(f"Threshold must be a number (e.g. -45), got “{threshold_value}”.")
            return
        if not (-80.0 <= threshold_db <= -10.0):
            self._set_status(
                f"Threshold {threshold_db:g} dB out of range (use -80 .. -10)."
            )
            return

        # Persist user choices to config.
        app.config.silence_threshold_db = threshold_db
        app.config.device_index = int(device_value)
        # Look up the friendly name for display later.
        for d in list_input_devices():
            if d.index == app.config.device_index:
                app.config.device_name = d.name
                break
        app.config.output_dir = str(output_path)
        app.config.save()

        # Push tracklist into AppState.
        details = self._details
        app.state.artist = details.artist
        app.state.album = details.album
        app.state.date = details.date
        app.state.mbid = details.mbid
        app.state.tracks = mbz.to_app_tracks(details)
        app.state.side_a_count = details.side_a_count
        app.state.phase = RipPhase.SETUP
        app.state.current_track_index = 0
        if app.artwork is not None:
            app.state.artwork_path = app.artwork.image_path

        # Step 8 wires the ripping screen. For now post a notice.
        self.post_message(self.BeginRipping())

    # Custom message so the app can react when wiring step 8/10.
    class BeginRipping(Message):
        pass
