"""RMS-based silence detection and track splitting.

The detector is a pure state machine over float32 audio blocks. It does not
touch the filesystem or threading primitives — callers feed blocks via
`feed()` and receive a list of events (`AUDIO`, `TRACK_START`, `TRACK_END`).
That lets us unit-test it with synthetic numpy arrays.

State machine:
    PRE_ROLL          waiting for first signal (silence is discarded)
    RECORDING         signal present, audio passed straight through
    TRAILING_SILENCE  signal dropped, blocks buffered while we decide

A trailing-silence run of >= `silence_duration_s` ends the current track —
provided the track is at least `min_track_duration_s` long (otherwise we
flush the buffer back and keep recording, treating it as a quiet passage).
The buffered silent blocks are *discarded* on a real split, so output WAVs
don't carry the inter-track gap.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Literal

import numpy as np


class DetectorState(str, Enum):
    PRE_ROLL = "pre_roll"
    RECORDING = "recording"
    TRAILING_SILENCE = "trailing_silence"


EventKind = Literal["audio", "track_start", "track_end"]


@dataclass
class DetectorEvent:
    kind: EventKind
    audio: np.ndarray | None = None  # only set for kind == "audio"


def block_rms_db(block: np.ndarray) -> float:
    """RMS of a (frames, channels) float32 block in dBFS.

    Returns -inf for true digital silence so callers can compare with `<` safely.
    """
    if block.size == 0:
        return -math.inf
    mean_sq = float(np.mean(np.square(block, dtype=np.float64)))
    if mean_sq <= 0.0:
        return -math.inf
    rms = math.sqrt(mean_sq)
    if rms <= 1e-12:
        return -math.inf
    return 20.0 * math.log10(rms)


class SilenceDetector:
    """Stateful track splitter. One instance per recording session (per side).

    Call `feed(block)` for every captured block, in order. Returned events
    describe what to do with the audio — write to the current track WAV,
    finalize it, or start a new one. Buffered silence at the end of a track
    is dropped from the output stream.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        threshold_db: float = -52.0,
        silence_duration_s: float = 2.5,
        min_track_duration_s: float = 20.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.sample_rate = sample_rate
        self.threshold_db = threshold_db
        self.silence_duration_s = silence_duration_s
        self.min_track_duration_s = min_track_duration_s

        self._state = DetectorState.PRE_ROLL
        self._silence_buf: deque[np.ndarray] = deque()
        self._silence_frames = 0  # frames buffered in _silence_buf
        self._track_frames = 0    # frames of audio already emitted for current track

    # ----- introspection (handy for tests / TUI debug) ---------------------

    @property
    def state(self) -> DetectorState:
        return self._state

    @property
    def current_track_seconds(self) -> float:
        return self._track_frames / self.sample_rate

    # ----- core API --------------------------------------------------------

    def feed(self, block: np.ndarray) -> list[DetectorEvent]:
        """Process one audio block. Returns events to act on, in order."""
        if block.ndim != 2:
            raise ValueError(f"expected (frames, channels) array, got shape {block.shape}")
        events: list[DetectorEvent] = []
        is_silent = block_rms_db(block) < self.threshold_db
        frames = block.shape[0]

        if self._state is DetectorState.PRE_ROLL:
            if is_silent:
                return events  # discard leading silence
            events.append(DetectorEvent("track_start"))
            events.append(DetectorEvent("audio", block))
            self._track_frames = frames
            self._state = DetectorState.RECORDING
            return events

        if self._state is DetectorState.RECORDING:
            if is_silent:
                # Start buffering; defer the decision until we know if this is a real gap.
                self._silence_buf.append(block)
                self._silence_frames = frames
                self._state = DetectorState.TRAILING_SILENCE
            else:
                events.append(DetectorEvent("audio", block))
                self._track_frames += frames
            return events

        # state is TRAILING_SILENCE
        if not is_silent:
            # Silence didn't last long enough — flush buffer back as part of the track.
            for buffered in self._silence_buf:
                events.append(DetectorEvent("audio", buffered))
                self._track_frames += buffered.shape[0]
            self._silence_buf.clear()
            self._silence_frames = 0
            events.append(DetectorEvent("audio", block))
            self._track_frames += frames
            self._state = DetectorState.RECORDING
            return events

        # Still silent — keep buffering and check thresholds.
        self._silence_buf.append(block)
        self._silence_frames += frames
        silent_seconds = self._silence_frames / self.sample_rate
        if silent_seconds < self.silence_duration_s:
            return events  # not yet a confirmed gap

        track_seconds = self._track_frames / self.sample_rate
        if track_seconds >= self.min_track_duration_s:
            # Confirmed inter-track silence: end the track, drop buffered silence.
            events.append(DetectorEvent("track_end"))
            self._silence_buf.clear()
            self._silence_frames = 0
            self._track_frames = 0
            self._state = DetectorState.PRE_ROLL
        else:
            # Track too short to split here (intro crackle, quiet passage).
            # Keep the buffered silence as part of the track and resume.
            for buffered in self._silence_buf:
                events.append(DetectorEvent("audio", buffered))
                self._track_frames += buffered.shape[0]
            self._silence_buf.clear()
            self._silence_frames = 0
            self._state = DetectorState.RECORDING
        return events

    def finalize(self) -> list[DetectorEvent]:
        """Call when the input stream ends (side flip / quit).

        Emits a `track_end` if currently recording so the last track gets closed.
        Any buffered trailing silence is discarded.
        """
        events: list[DetectorEvent] = []
        if self._state in (DetectorState.RECORDING, DetectorState.TRAILING_SILENCE):
            if self._track_frames > 0:
                events.append(DetectorEvent("track_end"))
        self._silence_buf.clear()
        self._silence_frames = 0
        self._track_frames = 0
        self._state = DetectorState.PRE_ROLL
        return events

    def force_split(self) -> list[DetectorEvent]:
        """Manually end the current track and start a new one immediately.

        For gapless transitions where the silence detector won't trigger.
        Any buffered trailing silence is discarded (treated as inter-track
        even though it never crossed the duration threshold). The new track
        starts in RECORDING state so the next block lands in it directly.
        """
        events: list[DetectorEvent] = []
        if self._state is DetectorState.PRE_ROLL:
            return events
        if self._track_frames == 0 and not self._silence_buf:
            return events
        events.append(DetectorEvent("track_end"))
        self._silence_buf.clear()
        self._silence_frames = 0
        self._track_frames = 0
        events.append(DetectorEvent("track_start"))
        self._state = DetectorState.RECORDING
        return events

    def reset(self) -> None:
        """Hard reset — discards in-flight state without emitting events."""
        self._silence_buf.clear()
        self._silence_frames = 0
        self._track_frames = 0
        self._state = DetectorState.PRE_ROLL


# --- helpers for tests / live wiring --------------------------------------


def feed_all(detector: SilenceDetector, blocks: Iterable[np.ndarray]) -> list[DetectorEvent]:
    """Convenience: feed an iterable of blocks and collect all events."""
    out: list[DetectorEvent] = []
    for b in blocks:
        out.extend(detector.feed(b))
    out.extend(detector.finalize())
    return out
