"""Shared in-memory state passed between screens and worker threads.

Owned by the Textual app. Mutations from non-UI threads must go through
`app.call_from_thread(...)`; this module only defines the data shapes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class TrackStatus(str, Enum):
    WAITING = "waiting"
    RECORDING = "recording"
    ENCODING = "encoding"
    DONE = "done"
    WARNING = "warning"


class Side(str, Enum):
    A = "A"
    B = "B"


class RipPhase(str, Enum):
    IDLE = "idle"
    SETUP = "setup"
    RIPPING = "ripping"
    AWAITING_FLIP = "awaiting_flip"
    COMPLETE = "complete"


@dataclass
class Track:
    number: int
    title: str
    duration_ms: int | None = None
    side: Side = Side.A
    status: TrackStatus = TrackStatus.WAITING
    output_path: Path | None = None
    actual_duration_ms: int | None = None
    warning: str | None = None


@dataclass
class AppState:
    artist: str = ""
    album: str = ""
    date: str = ""  # release year (string for tag compatibility)
    mbid: str | None = None
    tracks: list[Track] = field(default_factory=list)
    current_track_index: int = 0
    side: Side = Side.A
    phase: RipPhase = RipPhase.IDLE
    artwork_path: Path | None = None
    side_a_count: int | None = None  # number of tracks on side A; rest are side B

    # Lock for cross-thread mutations of mutable fields (mostly `tracks`).
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def current_track(self) -> Track | None:
        if 0 <= self.current_track_index < len(self.tracks):
            return self.tracks[self.current_track_index]
        return None
