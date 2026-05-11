"""Step-8 smoke test: launch the app into the ripping screen and exercise it.

Uses Textual's headless test mode. We monkey-patch `CaptureStream` so no
audio device is required, and inject synthetic blocks into the queue at
~real time (compressed). Verifies:
- screen composes
- VU updates from worker state
- track_started / track_finished events drive tracklist + progress
- side-A complete triggers flip modal
- after Resume, side B tracks rip and stream stops cleanly
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

# Force fixture mode so setup uses bundled MB JSON.
os.environ["JACK_USE_FIXTURE"] = "1"

# Patch CaptureStream BEFORE jack modules import it.
from jack.audio import capture as capture_mod  # noqa: E402

SR = 48000
CHANNELS = 2
BLOCK = 1024


class FakeStream:
    """Stand-in for sounddevice.CaptureStream. Audio is injected via push_block()."""

    def __init__(self, device_index, *, sample_rate=None, channels=2, **_kw):
        self.device_index = device_index
        self.sample_rate = sample_rate or SR
        self.channels = channels
        self.queue = queue.Queue(maxsize=512)
        self.active = False
        self.dropped_blocks = 0
        self._on_xrun = None

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def push_block(self, block):
        self.queue.put(block)


capture_mod.CaptureStream = FakeStream  # type: ignore[assignment]

# Now safe to import the rest.
from jack.tui.app import JackApp  # noqa: E402
from jack.tui.screens.ripping import RippingScreen  # noqa: E402
from jack.tui.screens.flip import FlipModal  # noqa: E402
from jack.tui.screens.setup import SetupScreen  # noqa: E402
from jack.state import TrackStatus  # noqa: E402


def tone(frames, freq, phase, amp=0.3):
    t = (np.arange(frames) + phase) / SR
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig] * CHANNELS, axis=1)


def silence(frames, noise=0.0005):
    return (np.random.standard_normal((frames, CHANNELS)) * noise).astype(np.float32)


def feed_blocks(stream: FakeStream, spec):
    """Feed a list of (kind, seconds, params) to the fake stream.

    Pushes blocks as fast as the consumer can take them — the controller's
    worker thread will iterate detection in order, so we don't need wall-time
    pacing. Sleep tiny amounts between segments so the UI tick has chances
    to refresh.
    """
    phase = 0
    for kind, secs, params in spec:
        total = int(secs * SR)
        while total > 0:
            n = min(BLOCK, total)
            block = tone(n, params.get("freq", 440), phase, params.get("amp", 0.3)) \
                if kind == "tone" else silence(n, params.get("noise", 0.0005))
            stream.push_block(block)
            phase += n if kind == "tone" else 0
            total -= n
            time.sleep(0.0005)


async def main() -> int:
    outdir = Path("/tmp/jack-rip-test")
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    app = JackApp()
    app.config.output_dir = str(outdir)
    app.config.device_index = 0
    app.config.channels = CHANNELS
    app.config.sample_rate = SR
    # Tighter detector knobs so the test runs fast.
    app.config.silence_duration_s = 0.4
    app.config.min_track_duration_s = 1.0

    # Pre-populate AppState from the bundled MB fixture so we can skip the
    # SetupScreen entirely — this test only exercises the rip pipeline.
    from jack.metadata import musicbrainz as mbz
    release_json = json.loads(
        (Path(__file__).resolve().parent.parent / "tests" / "fixtures"
         / "mb_release_dsotm_vinyl.json").read_text()
    )
    details = mbz.parse_release(release_json)
    app.state.artist = details.artist
    app.state.album = details.album
    app.state.date = details.date
    app.state.mbid = details.mbid
    app.state.tracks = mbz.to_app_tracks(details)
    app.state.side_a_count = details.side_a_count
    app.artwork = None  # exercise the fallback artwork path

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        # Replace SetupScreen with RippingScreen directly
        await app.switch_screen(RippingScreen())
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, RippingScreen):
                break
        assert isinstance(app.screen, RippingScreen), f"got {type(app.screen)}"
        print("on RippingScreen")

        # The controller now holds our FakeStream. Push 2 side-A tracks worth
        # of audio: tone, silence, tone, silence, (then 5 tracks total to hit
        # SIDE_A_COMPLETE). Use short tracks to keep the test fast.
        controller = app.screen._controller
        assert controller is not None
        fake_stream: FakeStream = controller._stream  # type: ignore[attr-defined]
        assert isinstance(fake_stream, FakeStream)

        side_a_spec = []
        for i in range(5):  # side A has 5 tracks
            side_a_spec.append(("tone", 1.5, {"freq": 440 + i * 50, "amp": 0.3}))
            side_a_spec.append(("silence", 0.7, {"noise": 0.0005}))

        def feeder():
            feed_blocks(fake_stream, side_a_spec)
        t = threading.Thread(target=feeder, daemon=True)
        t.start()

        # Let the controller drain the queue and emit events.
        for _ in range(200):
            await pilot.pause()
            if isinstance(app.screen, FlipModal):
                break
        # After 5 side-A tracks finish, the controller emits SIDE_A_COMPLETE
        # which pushes FlipModal.
        # Drain everything else
        t.join(timeout=5)
        # Pump until SIDE_A_COMPLETE is observed
        for _ in range(40):
            await pilot.pause()
            if isinstance(app.screen, FlipModal):
                break
        print(f"after side A: state.current_index={app.state.current_track_index}")
        print(f"flip modal shown: {isinstance(app.screen, FlipModal)}")
        assert isinstance(app.screen, FlipModal), \
            f"expected FlipModal, got {type(app.screen).__name__}"

        # Resume to side B
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RippingScreen)
        print(f"resumed, side now = {app.state.side.value}")
        assert app.state.side.value == "B"

        # Feed side B (5 more tracks)
        side_b_spec = []
        for i in range(5):
            side_b_spec.append(("tone", 1.5, {"freq": 660 + i * 50, "amp": 0.3}))
            side_b_spec.append(("silence", 0.7, {"noise": 0.0005}))
        t2 = threading.Thread(target=lambda: feed_blocks(fake_stream, side_b_spec), daemon=True)
        t2.start()
        for _ in range(200):
            await pilot.pause()
            done_count = sum(
                1 for t in app.state.tracks
                if t.status in (TrackStatus.DONE, TrackStatus.WARNING)
            )
            if done_count >= 10:
                break
        t2.join(timeout=5)

        # Wait for stop / final encodes
        for _ in range(40):
            await pilot.pause()

        statuses = [t.status.value for t in app.state.tracks]
        done = sum(1 for s in statuses if s in ("done", "warning"))
        print("final track statuses:", statuses)
        print(f"done count: {done}/10")
        assert done == 10, f"expected all 10 ripped, got {done}: {statuses}"

        # Check files exist
        flacs = sorted(outdir.rglob("*.flac"))
        print(f"flacs written: {len(flacs)}")
        for f in flacs:
            print(f"  {f.relative_to(outdir)}  ({f.stat().st_size:,} bytes)")
        assert len(flacs) == 10

        # Sanity: every file is a valid FLAC
        import soundfile as sf
        for f in flacs:
            info = sf.info(str(f))
            assert info.format == "FLAC"
            assert info.subtype == "PCM_24"

        # After all tracks finish, RippingScreen should switch to CompletionScreen.
        from jack.tui.screens.complete import CompletionScreen
        for _ in range(40):
            await pilot.pause()
            if isinstance(app.screen, CompletionScreen):
                break
        print(f"final screen: {type(app.screen).__name__}")
        assert isinstance(app.screen, CompletionScreen), \
            f"expected CompletionScreen, got {type(app.screen).__name__}"

        from textual.widgets import DataTable, Static
        ctbl = app.screen.query_one("#complete-tracks", DataTable)
        print(f"completion table rows: {ctbl.row_count}")
        assert ctbl.row_count == 10

        # Rip another → back to SetupScreen, fresh state
        await pilot.press("r")
        await pilot.pause()
        from jack.tui.screens.setup import SetupScreen as _Setup
        assert isinstance(app.screen, _Setup), f"got {type(app.screen).__name__}"
        assert app.state.album == "", f"state not reset: album={app.state.album!r}"
        assert len(app.state.tracks) == 0
        print("rip-another reset OK")
        print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
