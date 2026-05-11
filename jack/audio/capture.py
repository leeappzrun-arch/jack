"""USB audio capture.

`sounddevice.InputStream` runs its callback in a PortAudio C thread; we never
do work there. The callback only `put_nowait()`s the raw float32 buffer into
a bounded queue. Python-side consumers (silence detector, VU meter) pull from
the queue on their own thread.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Frames per callback. ~23 ms at 44.1 kHz — small enough for responsive VU,
# big enough that we don't drown the queue.
BLOCKSIZE = 1024

# Max queued blocks (~23 ms × 256 = ~6 s of audio). If consumers stall longer
# than this we drop, log, and surface via `dropped_blocks`.
QUEUE_MAXSIZE = 256


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: int

    @property
    def is_usb(self) -> bool:
        return "usb" in self.name.lower()


def list_input_devices() -> list[DeviceInfo]:
    """All devices with at least one input channel."""
    out: list[DeviceInfo] = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            out.append(
                DeviceInfo(
                    index=idx,
                    name=dev["name"],
                    max_input_channels=dev["max_input_channels"],
                    default_samplerate=int(dev["default_samplerate"]),
                )
            )
    return out


def list_usb_input_devices() -> list[DeviceInfo]:
    return [d for d in list_input_devices() if d.is_usb]


class CaptureStream:
    """Wraps `sd.InputStream`. Pushes float32 (frames, channels) blocks to `queue`.

    Lifecycle: `start()` → `stop()`. Safe to call `stop()` from any thread.
    """

    def __init__(
        self,
        device_index: int,
        *,
        sample_rate: int | None = None,
        channels: int = 2,
        blocksize: int = BLOCKSIZE,
        queue_maxsize: int = QUEUE_MAXSIZE,
        on_xrun: Callable[[str], None] | None = None,
    ) -> None:
        self.device_index = device_index
        # Resolved at start() if None: falls back to the device's native rate.
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._on_xrun = on_xrun
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self.dropped_blocks = 0

    # PortAudio thread — keep this tight.
    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        if status:
            # Input over/underflow. Don't raise from the audio thread.
            msg = str(status)
            logger.warning("PortAudio status: %s", msg)
            if self._on_xrun is not None:
                try:
                    self._on_xrun(msg)
                except Exception:
                    logger.exception("on_xrun handler failed")
        try:
            # Copy: PortAudio reuses the buffer after the callback returns.
            self.queue.put_nowait(indata.copy())
        except queue.Full:
            self.dropped_blocks += 1
            # Drop oldest to keep latency bounded.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(indata.copy())
            except (queue.Empty, queue.Full):
                pass

    def _resolve_sample_rate(self) -> int:
        """Return a sample rate PortAudio will actually accept for this device.

        If the caller didn't pin a rate, use the device's reported default.
        If they did pin one, pre-validate via check_input_settings() so we
        fail loudly here rather than during the open() call.
        """
        dev = sd.query_devices(self.device_index)
        native = int(dev["default_samplerate"])
        if self.sample_rate is None:
            return native
        try:
            sd.check_input_settings(
                device=self.device_index,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
            )
            return self.sample_rate
        except sd.PortAudioError as e:
            raise sd.PortAudioError(
                f"Device {self.device_index} ({dev['name']}) does not support "
                f"{self.sample_rate} Hz. Native rate is {native} Hz. "
                f"Set sample_rate=None to use the native rate."
            ) from e

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            rate = self._resolve_sample_rate()
            self._stream = sd.InputStream(
                device=self.device_index,
                samplerate=rate,
                channels=self.channels,
                dtype="float32",
                blocksize=self.blocksize,
                callback=self._callback,
            )
            self._stream.start()
            self.sample_rate = rate  # record the resolved rate
            logger.info(
                "Capture started: device=%d sr=%d ch=%d block=%d",
                self.device_index,
                rate,
                self.channels,
                self.blocksize,
            )

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()
            logger.info("Capture stopped (dropped_blocks=%d)", self.dropped_blocks)

    @property
    def active(self) -> bool:
        return self._stream is not None and self._stream.active

    def __enter__(self) -> "CaptureStream":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
