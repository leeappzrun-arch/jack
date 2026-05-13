"""Orchestrates the live rip: capture → silence detection → temp WAV → FLAC.

Threading model:

    PortAudio C thread     → CaptureStream.queue (numpy blocks)
              │
              ▼
    `_worker` Python thread reads queue, computes block RMS for the VU
    meter, and feeds blocks to `SilenceDetector`. On `track_end`, the
    finished WAV is handed to a ThreadPoolExecutor that encodes it to
    FLAC in the background — the worker keeps draining the queue so we
    don't drop audio while encoding.

The controller never touches Textual widgets. It posts thread-safe events
to its `on_event` callback; the screen is responsible for marshalling
those into `call_from_thread` UI updates.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np

from jack.audio.capture import CaptureStream
from jack.audio.encoder import (
    TempWavWriter,
    TrackMetadata,
    encode_to_flac,
    output_path_for,
)
from jack.audio.silence import SilenceDetector, block_rms_db
from jack.metadata.artwork import Artwork
from jack.state import (
    AppState,
    Side,
    Track,
    TrackStatus,
    disc_for_side,
    total_discs_for_sides,
)

logger = logging.getLogger(__name__)

MAX_PEAK_HOLD_S = 0.6  # how long a peak value lingers in the VU meter


class EventKind(str, Enum):
    """Events emitted from the worker thread to the UI."""
    READY = "ready"                  # capture started, listening
    TRACK_STARTED = "track_started"  # detector saw audio, recording into temp WAV
    TRACK_FINISHED = "track_finished"  # FLAC encode complete (or failed)
    SIDE_COMPLETE = "side_complete"  # expected track count for current side hit
    PAUSED = "paused"                # capture paused (waiting for flip)
    RESUMED = "resumed"              # capture resumed (next side)
    STOPPED = "stopped"              # capture stopped (user quit / done)
    ERROR = "error"


@dataclass
class ControllerEvent:
    kind: EventKind
    track_index: int | None = None
    message: str | None = None
    output_path: Path | None = None
    actual_duration_ms: int | None = None
    # For SIDE_COMPLETE: the side that just finished and the one to flip to.
    completed_side: Side | None = None
    next_side: Side | None = None


class RipController:
    """Drives capture → split → encode. Single-use per album (don't restart)."""

    def __init__(
        self,
        *,
        state: AppState,
        device_index: int,
        sample_rate: int | None,
        channels: int,
        monitor_device_index: int | None,
        threshold_db: float,
        silence_duration_s: float,
        min_track_duration_s: float,
        min_track_fraction_of_expected: float,
        output_dir: Path,
        artwork: Artwork | None,
        on_event,
    ) -> None:
        self.state = state
        self.output_dir = output_dir
        self.artwork = artwork
        self._on_event = on_event
        self._static_min_track_s = min_track_duration_s
        self._min_fraction_of_expected = min_track_fraction_of_expected

        self._stream = CaptureStream(
            device_index,
            sample_rate=sample_rate,
            channels=channels,
            monitor_device_index=monitor_device_index,
        )
        # The detector instance is replaced after a side flip so PRE_ROLL is fresh.
        self._detector_kwargs = dict(
            threshold_db=threshold_db,
            silence_duration_s=silence_duration_s,
            min_track_duration_s=min_track_duration_s,
        )
        self._detector: SilenceDetector | None = None

        self._writer: TempWavWriter | None = None
        self._encoder_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jack-encode")
        self._encode_futures: list[Future] = []

        self._stop = threading.Event()
        self._paused = threading.Event()
        self._force_split = threading.Event()
        self._worker_thread: threading.Thread | None = None

        # VU state, written by worker thread, read by UI timer.
        self._vu_rms_db = -math.inf
        self._vu_peak_db = -math.inf
        self._vu_peak_hold_until = 0.0

    # ----- public API ------------------------------------------------------

    @property
    def sample_rate(self) -> int:
        return self._stream.sample_rate

    def vu_levels(self) -> tuple[float, float]:
        """Latest (rms_db, peak_db). Safe to call from any thread."""
        return self._vu_rms_db, self._vu_peak_db

    def start(self) -> None:
        """Open the audio device and start the worker thread."""
        self._detector = SilenceDetector(
            sample_rate=44100,  # placeholder; replaced after stream.start()
            **self._detector_kwargs,
        )
        self._stream.start()
        # Re-init detector with the actual resolved rate.
        self._detector = SilenceDetector(
            sample_rate=self._stream.sample_rate,
            **self._detector_kwargs,
        )
        self._worker_thread = threading.Thread(
            target=self._worker, name="jack-rip-worker", daemon=True
        )
        self._worker_thread.start()

    def pause(self) -> None:
        self._paused.set()

    def force_split(self) -> None:
        """Request a manual track boundary at the next worker iteration.

        Used for gapless transitions where silence detection never fires.
        No-op if paused or not currently recording a track.
        """
        self._force_split.set()

    def resume_next_side(self) -> None:
        """Advance to the next side, reset detector, resume recording.

        The next side is derived from the track we're about to record: each
        track's `side` was assigned by the MB adapter, so we just read it.
        """
        track = self.state.current_track()
        with self.state.lock:
            if track is not None:
                self.state.side = track.side
        if self._detector is not None:
            self._detector.reset()
        self._paused.clear()

    def stop(self) -> None:
        """Stop capture, finalize any in-flight track, wait for encodings to finish."""
        self._stop.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5.0)
        self._stream.stop()
        # Encode whatever was buffered when we stopped.
        self._finalize_in_flight_track()
        for fut in self._encode_futures:
            try:
                fut.result(timeout=60)
            except Exception:
                logger.exception("encoder future failed during shutdown")
        self._encoder_pool.shutdown(wait=True)

    # ----- worker thread ---------------------------------------------------

    def _worker(self) -> None:
        assert self._detector is not None
        while not self._stop.is_set():
            try:
                block = self._stream.queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # VU update (always, even while paused so user sees the level).
            self._update_vu(block)

            if self._paused.is_set():
                continue

            if self._force_split.is_set():
                self._force_split.clear()
                try:
                    for ev in self._detector.force_split():
                        self._dispatch(ev)
                except Exception as e:
                    logger.exception("detector.force_split failed")
                    self._emit(EventKind.ERROR, message=str(e))

            try:
                events = self._detector.feed(block)
            except Exception as e:
                logger.exception("detector.feed failed")
                self._emit(EventKind.ERROR, message=str(e))
                continue

            for ev in events:
                self._dispatch(ev)

    def _dispatch(self, ev) -> None:
        """Handle one DetectorEvent inside the worker thread."""
        if ev.kind == "track_start":
            self._open_writer()
        elif ev.kind == "audio":
            if self._writer is not None and ev.audio is not None:
                try:
                    self._writer.append(ev.audio)
                except Exception as e:
                    logger.exception("writer.append failed")
                    self._emit(EventKind.ERROR, message=str(e))
        elif ev.kind == "track_end":
            self._close_writer_and_encode()

    def _open_writer(self) -> None:
        track = self._current_track()
        if track is None:
            logger.warning("track_start with no remaining track in state; ignoring")
            return
        try:
            self._writer = TempWavWriter(
                sample_rate=self._stream.sample_rate,
                channels=self._stream.channels,
            )
        except Exception as e:
            logger.exception("failed to open temp WAV")
            self._emit(EventKind.ERROR, message=str(e))
            return
        # Tune the detector's per-track minimum from MB's expected duration so
        # mid-song silences don't trigger a false split. If MB has no duration,
        # the static floor still applies.
        if self._detector is not None:
            expected_s = (track.duration_ms or 0) / 1000.0
            dynamic_min = expected_s * self._min_fraction_of_expected
            self._detector.min_track_duration_s = max(
                self._static_min_track_s, dynamic_min
            )
        with self.state.lock:
            track.status = TrackStatus.RECORDING
        self._emit(EventKind.TRACK_STARTED, track_index=self._current_index())

    def _close_writer_and_encode(self) -> None:
        if self._writer is None:
            return
        writer = self._writer
        self._writer = None
        track = self._current_track()
        if track is None:
            writer.abort()
            return
        wav_path, frames = writer.close()
        actual_duration_ms = int(frames / writer.sample_rate * 1000)
        with self.state.lock:
            track.status = TrackStatus.ENCODING
            track.actual_duration_ms = actual_duration_ms
            current_idx = self.state.current_track_index

        # Submit the encode in the background so the worker can keep reading the queue.
        fut = self._encoder_pool.submit(
            self._encode_one, wav_path, current_idx, actual_duration_ms
        )
        self._encode_futures.append(fut)

        # Advance the current-track pointer immediately so the next detected
        # track gets the right metadata.
        with self.state.lock:
            self.state.current_track_index = current_idx + 1
            new_idx = self.state.current_track_index
            boundaries = self.state.side_boundaries()
            total_tracks = len(self.state.tracks)
            completed_side = self.state.tracks[current_idx].side
            next_side = (
                self.state.tracks[new_idx].side if new_idx < total_tracks else None
            )

        # SIDE_COMPLETE fires at every internal boundary (i.e. not the final one).
        # The detector hasn't seen audio yet for the next side — the flip flow
        # will pause capture and reset the detector before any of that matters.
        at_internal_boundary = (
            new_idx in boundaries and new_idx < total_tracks
        )
        if at_internal_boundary:
            self._emit(
                EventKind.SIDE_COMPLETE,
                track_index=current_idx,
                completed_side=completed_side,
                next_side=next_side,
            )
        elif new_idx >= total_tracks:
            # All expected tracks captured — stop on the UI side.
            self._stop.set()

    def _encode_one(self, wav_path: Path, track_index: int, actual_duration_ms: int) -> None:
        """Runs on the encoder pool. Updates state + emits TRACK_FINISHED."""
        try:
            track = self.state.tracks[track_index]
            disc_no = disc_for_side(track.side)
            total_discs = total_discs_for_sides(self.state.sides_order) or 1
            # Per-disc track number (1..N within the disc) is friendlier in tags
            # than a global 1..total — e.g. disc 2 track 1 instead of track 11.
            tracknumber, totaltracks = _disc_local_numbering(
                self.state, track_index, disc_no
            )
            metadata = TrackMetadata(
                artist=self.state.artist,
                album=self.state.album,
                title=track.title or f"Track {track.number}",
                tracknumber=tracknumber,
                totaltracks=totaltracks,
                discnumber=disc_no,
                totaldiscs=total_discs,
                date=_year_of(self.state.date),
                albumartist=self.state.artist,
            )
            out_path = output_path_for(metadata, output_dir=self.output_dir)
            encode_to_flac(
                wav_path,
                out_path,
                metadata,
                artwork=self.artwork,
                delete_source=True,
            )
            with self.state.lock:
                track.output_path = out_path
                track.status = TrackStatus.DONE
                track.warning = _duration_warning(track.duration_ms, actual_duration_ms)
                if track.warning:
                    track.status = TrackStatus.WARNING
            self._emit(
                EventKind.TRACK_FINISHED,
                track_index=track_index,
                output_path=out_path,
                actual_duration_ms=actual_duration_ms,
            )
        except Exception as e:
            logger.exception("encoding track %d failed", track_index)
            with self.state.lock:
                self.state.tracks[track_index].status = TrackStatus.WARNING
                self.state.tracks[track_index].warning = str(e)
            self._emit(
                EventKind.TRACK_FINISHED,
                track_index=track_index,
                message=str(e),
            )

    def _finalize_in_flight_track(self) -> None:
        """If the worker was mid-track when stop() arrived, encode what we have."""
        if self._writer is None or self._detector is None:
            return
        events = self._detector.finalize()
        for ev in events:
            self._dispatch(ev)
        # If the detector didn't produce a track_end but a writer is still open
        # (e.g. very short track), abort it.
        if self._writer is not None:
            self._writer.abort()
            self._writer = None

    # ----- helpers --------------------------------------------------------

    def _current_track(self) -> Track | None:
        return self.state.current_track()

    def _current_index(self) -> int:
        return self.state.current_track_index

    def _emit(self, kind: EventKind, **kwargs) -> None:
        try:
            self._on_event(ControllerEvent(kind=kind, **kwargs))
        except Exception:
            logger.exception("on_event handler raised")

    def _update_vu(self, block: np.ndarray) -> None:
        rms = block_rms_db(block)
        peak_lin = float(np.max(np.abs(block)))
        peak = 20.0 * math.log10(peak_lin) if peak_lin > 1e-9 else -math.inf
        self._vu_rms_db = rms
        now = time.monotonic()
        if math.isfinite(peak) and (peak > self._vu_peak_db or now > self._vu_peak_hold_until):
            self._vu_peak_db = peak
            self._vu_peak_hold_until = now + MAX_PEAK_HOLD_S


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _disc_local_numbering(state: AppState, track_index: int, disc_no: int) -> tuple[int, int]:
    """Return (tracknumber, totaltracks) scoped to the disc this track is on.

    If sides_order is empty (single-side, no MB info), the album is treated
    as one disc and we just use the global 1..N numbering.
    """
    if not state.sides_order:
        return track_index + 1, len(state.tracks)
    on_disc = [
        i for i, t in enumerate(state.tracks)
        if disc_for_side(t.side) == disc_no
    ]
    try:
        local = on_disc.index(track_index) + 1
    except ValueError:
        local = track_index + 1
    return local, len(on_disc)


def _year_of(date: str) -> str:
    if not date:
        return ""
    return date.split("-")[0]


def _duration_warning(
    expected_ms: int | None,
    actual_ms: int,
    tolerance: float = 0.15,
) -> str | None:
    """Return a warning string if actual diverges from expected by more than tolerance."""
    if not expected_ms:
        return None
    delta = abs(actual_ms - expected_ms) / expected_ms
    if delta > tolerance:
        return (
            f"length {actual_ms / 1000:.1f}s differs from MB expected "
            f"{expected_ms / 1000:.1f}s by {delta * 100:.0f}%"
        )
    return None
