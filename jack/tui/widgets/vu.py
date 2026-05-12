"""Horizontal text VU meter shared by the ripping and test screens."""
from __future__ import annotations

import math

from textual.reactive import reactive
from textual.widgets import Static


class VUMeter(Static):
    """Horizontal text VU. Update via `set_levels(rms_db, peak_db)`.

    Green 0..70% of the bar, yellow 70..85%, red 85..100%. With FLOOR_DB=-60
    and WIDTH=50 that maps to roughly -18 dBFS (yellow start) and -9 dBFS
    (red start), so red means peaks are at imminent-clipping levels.
    """

    DEFAULT_CSS = """
    VUMeter {
        height: 3;
        padding: 0 2;
        content-align: left middle;
    }
    """

    rms_db = reactive(-math.inf, layout=False)
    peak_db = reactive(-math.inf, layout=False)

    FLOOR_DB = -60.0
    WIDTH = 50

    def set_levels(self, rms_db: float, peak_db: float) -> None:
        self.rms_db = rms_db
        self.peak_db = peak_db

    def watch_rms_db(self, _old: float, _new: float) -> None:
        self.refresh()

    def watch_peak_db(self, _old: float, _new: float) -> None:
        self.refresh()

    def render(self) -> str:
        def cells(db: float) -> int:
            if not math.isfinite(db):
                return 0
            frac = (db - self.FLOOR_DB) / (-self.FLOOR_DB)
            return max(0, min(self.WIDTH, int(frac * self.WIDTH)))

        rms_n = cells(self.rms_db)
        peak_n = cells(self.peak_db)
        bar_chars: list[str] = []
        for i in range(self.WIDTH):
            if i < rms_n:
                if i >= int(self.WIDTH * 0.85):
                    bar_chars.append("[red]█[/]")
                elif i >= int(self.WIDTH * 0.7):
                    bar_chars.append("[yellow]█[/]")
                else:
                    bar_chars.append("[green]█[/]")
            elif i < peak_n:
                bar_chars.append("[white]▏[/]")
            else:
                bar_chars.append("·")
        bar = "".join(bar_chars)
        rms_s = f"{self.rms_db:+6.1f}" if math.isfinite(self.rms_db) else "  -inf"
        peak_s = f"{self.peak_db:+6.1f}" if math.isfinite(self.peak_db) else "  -inf"
        return f"VU  {bar}  peak {peak_s} dB  rms {rms_s} dB"
