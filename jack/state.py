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
    C = "C"
    D = "D"
    E = "E"
    F = "F"


def disc_for_side(side: Side | str) -> int:
    """A/B → 1, C/D → 2, E/F → 3."""
    letter = side.value if isinstance(side, Side) else side
    return (ord(letter.upper()) - ord("A")) // 2 + 1


def total_discs_for_sides(sides: list[Side] | list[str]) -> int:
    if not sides:
        return 1
    return max(disc_for_side(s) for s in sides)


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
    # Sides present, in playback order (e.g. ["A","B","C","D"]).
    # Empty if MB didn't supply per-side info — UI then runs as a single side.
    sides_order: list[Side] = field(default_factory=list)
    # Track count for each side in `sides_order`. Same length as sides_order.
    side_counts: list[int] = field(default_factory=list)

    # Lock for cross-thread mutations of mutable fields (mostly `tracks`).
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def current_track(self) -> Track | None:
        if 0 <= self.current_track_index < len(self.tracks):
            return self.tracks[self.current_track_index]
        return None

    def side_boundaries(self) -> list[int]:
        """Cumulative track index where each side *ends* (exclusive).

        For sides_order=[A,B,C,D] and side_counts=[5,4,5,4] returns [5,9,14,18].
        Empty list if side info is unknown.
        """
        if not self.side_counts:
            return []
        out: list[int] = []
        running = 0
        for n in self.side_counts:
            running += n
            out.append(running)
        return out

    def side_index_for_track(self, track_index: int) -> int:
        """Which side (0-based index into sides_order) a given track belongs to."""
        for i, end in enumerate(self.side_boundaries()):
            if track_index < end:
                return i
        return max(0, len(self.sides_order) - 1)
