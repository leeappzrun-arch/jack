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

# Live-monitor queue holds only a handful of blocks (~92 ms at 1024 frames/44.1 kHz).
# Tight cap on purpose: monitor latency must stay low, and a stalled output
# device must never backpressure the rip path.
MONITOR_QUEUE_MAXSIZE = 4


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


@dataclass(frozen=True)
class OutputDeviceInfo:
    index: int
    name: str
    max_output_channels: int
    default_samplerate: int


def list_output_devices() -> list[OutputDeviceInfo]:
    """All devices with at least one output channel."""
    out: list[OutputDeviceInfo] = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] > 0:
            out.append(
                OutputDeviceInfo(
                    index=idx,
                    name=dev["name"],
                    max_output_channels=dev["max_output_channels"],
                    default_samplerate=int(dev["default_samplerate"]),
                )
            )
    return out


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
        monitor_device_index: int | None = None,
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

        # Live monitor (optional). Same SR/channels as the input — no resampling.
        self.monitor_device_index = monitor_device_index
        self._monitor_stream: sd.OutputStream | None = None
        self._monitor_queue: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=MONITOR_QUEUE_MAXSIZE
        )
        self.monitor_dropped = 0

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
        # Copy once: PortAudio reuses the buffer after the callback returns,
        # and both the rip queue and the monitor queue need their own ownership.
        block = indata.copy()
        try:
            self.queue.put_nowait(block)
        except queue.Full:
            self.dropped_blocks += 1
            # Drop oldest to keep latency bounded.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(block)
            except (queue.Empty, queue.Full):
                pass

        # Monitor tee. Must never block the rip path: if the output stream
        # stalls and the queue fills, we drop the monitor block silently.
        if self._monitor_stream is not None:
            try:
                self._monitor_queue.put_nowait(block)
            except queue.Full:
                self.monitor_dropped += 1

    # PortAudio thread (output side).
    def _monitor_callback(self, outdata: np.ndarray, frames: int, time, status) -> None:
        if status:
            logger.warning("PortAudio monitor status: %s", str(status))
        try:
            block = self._monitor_queue.get_nowait()
        except queue.Empty:
            outdata.fill(0)
            return
        if block.shape == outdata.shape:
            outdata[:] = block
        elif block.shape[0] == frames and block.ndim == outdata.ndim:
            # Channel-count mismatch (e.g. mono input → stereo output): duplicate.
            if block.shape[1] == 1 and outdata.shape[1] >= 1:
                outdata[:] = np.repeat(block, outdata.shape[1], axis=1)
            else:
                outdata.fill(0)
        else:
            outdata.fill(0)

    def _open_monitor(self, sample_rate: int) -> None:
        """Best-effort. Failure to open the monitor never fails the rip."""
        if self.monitor_device_index is None:
            return
        dev = sd.query_devices(self.monitor_device_index)
        out_channels = min(self.channels, int(dev["max_output_channels"]))
        if out_channels < 1:
            logger.warning(
                "Monitor device %d (%s) has no output channels; skipping",
                self.monitor_device_index, dev["name"],
            )
            return
        try:
            sd.check_output_settings(
                device=self.monitor_device_index,
                samplerate=sample_rate,
                channels=out_channels,
                dtype="float32",
            )
        except sd.PortAudioError as e:
            logger.warning(
                "Monitor device %d (%s) does not accept %d Hz / %d ch; "
                "skipping monitor: %s",
                self.monitor_device_index, dev["name"], sample_rate, out_channels, e,
            )
            return
        try:
            stream = sd.OutputStream(
                device=self.monitor_device_index,
                samplerate=sample_rate,
                channels=out_channels,
                dtype="float32",
                blocksize=self.blocksize,
                callback=self._monitor_callback,
            )
            stream.start()
        except Exception:
            logger.exception("opening monitor output failed; rip continues without it")
            return
        self._monitor_stream = stream
        logger.info(
            "Monitor started: device=%d sr=%d ch=%d",
            self.monitor_device_index, sample_rate, out_channels,
        )

    def _close_monitor(self) -> None:
        stream = self._monitor_stream
        self._monitor_stream = None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logger.exception("stopping monitor stream failed")
        finally:
            try:
                stream.close()
            except Exception:
                logger.exception("closing monitor stream failed")
            logger.info(
                "Monitor stopped (dropped_blocks=%d)", self.monitor_dropped,
            )

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
            # Open the monitor at the resolved input rate. Order matters:
            # the rip stream is the source of truth — monitor follows.
            self._open_monitor(rate)

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        # Close monitor first so its callback stops pulling from the queue
        # before we tear down the input stream.
        self._close_monitor()
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
