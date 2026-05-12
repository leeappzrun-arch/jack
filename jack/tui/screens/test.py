"""Test screen: live VU meter for tuning input gain before a rip.

No file output — just opens a CaptureStream, drains blocks on a background
thread, and feeds RMS/peak to the VU meter. Optional monitor output lets
the user listen while they adjust the preamp.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import TYPE_CHECKING

import numpy as np
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Select, Static

from jack.audio.capture import CaptureStream, list_input_devices, list_output_devices
from jack.audio.silence import block_rms_db
from jack.tui.widgets.vu import VUMeter

if TYPE_CHECKING:
    from jack.tui.app import JackApp

logger = logging.getLogger(__name__)

PEAK_HOLD_S = 0.6


class TestScreen(Screen):
    """Live level check. Pick a device, hit Start, watch the meter."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("space", "toggle", "Start / Stop"),
        Binding("q", "back", "Back"),
    ]

    class Back(Message):
        pass

    def __init__(self) -> None:
        super().__init__()
        self._stream: CaptureStream | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._vu_rms_db = -math.inf
        self._vu_peak_db = -math.inf
        self._vu_peak_hold_until = 0.0
        self._ui_timer = None

    # ---- compose --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="test-grid"):
            yield Static(
                "Pick your input device, press Start, and adjust your preamp "
                "until peaks sit in the upper green / lower yellow.",
                id="test-help",
            )
            with Horizontal(classes="row"):
                yield Static("Device:", classes="label")
                yield Select(options=[], prompt="Detecting devices...", id="test-device")
            with Horizontal(classes="row"):
                yield Static("Monitor:", classes="label")
                yield Select(
                    options=[],
                    prompt="(none — silent)",
                    id="test-monitor",
                    allow_blank=True,
                )
            yield VUMeter(id="test-vu")
            yield Static("", id="test-status")
            with Horizontal(classes="button-row"):
                yield Button("Back  (Esc)", id="test-back")
                yield Button("Start  (Space)", id="test-toggle", variant="primary")
        yield Footer()

    # ---- mount ----------------------------------------------------------

    def on_mount(self) -> None:
        app: "JackApp" = self.app  # type: ignore[assignment]

        # Input devices
        select = self.query_one("#test-device", Select)
        try:
            devices = list_input_devices()
        except Exception as e:
            logger.exception("device enumeration failed")
            select.prompt = f"device error: {e}"
            devices = []
        if devices:
            select.set_options(
                [
                    (f"[{d.index}] {d.name}  ({d.max_input_channels}ch, "
                     f"{d.default_samplerate} Hz)", d.index)
                    for d in devices
                ]
            )
            select.prompt = "Select input device"
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

        # Output devices (for optional monitor)
        monitor = self.query_one("#test-monitor", Select)
        try:
            outs = list_output_devices()
        except Exception as e:
            logger.exception("output device enumeration failed")
            monitor.prompt = f"monitor error: {e}"
            outs = []
        if outs:
            monitor.set_options(
                [
                    (f"[{d.index}] {d.name}  ({d.max_output_channels}ch, "
                     f"{d.default_samplerate} Hz)", d.index)
                    for d in outs
                ]
            )
            preferred_mon = app.config.monitor_device_index
            if preferred_mon is not None and preferred_mon in {d.index for d in outs}:
                monitor.value = preferred_mon

        self._set_status("Idle. Pick a device and press Start.")

    def on_unmount(self) -> None:
        self._stop_capture()

    # ---- actions --------------------------------------------------------

    def action_back(self) -> None:
        self.post_message(self.Back())

    def action_toggle(self) -> None:
        if self._stream is None:
            self._start_capture()
        else:
            self._stop_capture()

    @on(Button.Pressed, "#test-back")
    def _on_back(self) -> None:
        self.action_back()

    @on(Button.Pressed, "#test-toggle")
    def _on_toggle(self) -> None:
        self.action_toggle()

    # ---- capture lifecycle ---------------------------------------------

    def _start_capture(self) -> None:
        if self._stream is not None:
            return
        device_value = self.query_one("#test-device", Select).value
        if device_value is Select.BLANK:
            self._set_status("[yellow]Pick an input device first.[/]")
            return
        monitor_value = self.query_one("#test-monitor", Select).value
        monitor_index = None if monitor_value is Select.BLANK else int(monitor_value)

        try:
            self._stream = CaptureStream(
                int(device_value),
                monitor_device_index=monitor_index,
            )
            self._stream.start()
        except Exception as e:
            logger.exception("test capture start failed")
            self._set_status(f"[red]Could not start capture: {e}[/]")
            self._stream = None
            return

        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="jack-test-reader",
            daemon=True,
        )
        self._reader_thread.start()
        self._ui_timer = self.set_interval(0.1, self._tick_ui)
        self.query_one("#test-toggle", Button).label = "Stop  (Space)"
        self._set_status(
            f"Listening at {self._stream.sample_rate} Hz. "
            "Adjust gain so peaks stay out of red."
        )

    def _stop_capture(self) -> None:
        if self._ui_timer is not None:
            self._ui_timer.stop()
            self._ui_timer = None
        self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                logger.exception("stream.stop in test screen failed")
        try:
            self.query_one("#test-toggle", Button).label = "Start  (Space)"
            self.query_one("#test-vu", VUMeter).set_levels(-math.inf, -math.inf)
            self._set_status("Stopped.")
        except Exception:
            pass  # screen tearing down

    def _reader_loop(self) -> None:
        """Drain capture blocks, update VU state. Pure compute — no UI calls."""
        assert self._stream is not None
        while not self._stop_event.is_set():
            try:
                block: np.ndarray = self._stream.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            rms = block_rms_db(block)
            peak_lin = float(np.max(np.abs(block)))
            peak = 20.0 * math.log10(peak_lin) if peak_lin > 1e-9 else -math.inf
            now = time.monotonic()
            self._vu_rms_db = rms
            if math.isfinite(peak) and (
                peak > self._vu_peak_db or now > self._vu_peak_hold_until
            ):
                self._vu_peak_db = peak
                self._vu_peak_hold_until = now + PEAK_HOLD_S

    def _tick_ui(self) -> None:
        try:
            self.query_one("#test-vu", VUMeter).set_levels(
                self._vu_rms_db, self._vu_peak_db
            )
        except Exception:
            pass

    # ---- helpers --------------------------------------------------------

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#test-status", Static).update(text)
        except Exception:
            pass
