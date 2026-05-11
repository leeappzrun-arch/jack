"""Step-2 smoke test: stream audio from a USB device for a few seconds and
print per-block peak / RMS so you can confirm signal is reaching the queue.

Usage:
    python scripts/capture_test.py            # auto-pick first USB input
    python scripts/capture_test.py 16         # explicit device index
    python scripts/capture_test.py 16 --seconds 5 --sr 44100
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from jack.audio.capture import CaptureStream, list_usb_input_devices


def rms_db(block: np.ndarray) -> float:
    """RMS of one block, expressed in dBFS. Returns -inf for silence."""
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    if rms <= 1e-9:
        return -math.inf
    return 20.0 * math.log10(rms)


def peak_db(block: np.ndarray) -> float:
    peak = float(np.max(np.abs(block)))
    if peak <= 1e-9:
        return -math.inf
    return 20.0 * math.log10(peak)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("device", nargs="?", type=int, help="device index (default: first USB)")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument(
        "--sr",
        type=int,
        default=None,
        help="sample rate (default: device native — recommended)",
    )
    ap.add_argument("--channels", type=int, default=2)
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    if args.device is None:
        usb = list_usb_input_devices()
        if not usb:
            print("No USB input devices found.")
            return 1
        device = usb[0].index
        print(f"Auto-picked USB device [{device}] {usb[0].name}")
    else:
        device = args.device

    rate_label = f"{args.sr} Hz" if args.sr else "native"
    print(f"Capturing {args.seconds}s at {rate_label}, {args.channels} ch...")
    stream = CaptureStream(device, sample_rate=args.sr, channels=args.channels)
    stream.start()
    print(f"  -> resolved sample rate: {stream.sample_rate} Hz")

    start = time.monotonic()
    block_count = 0
    silent_floor = -90.0
    try:
        while time.monotonic() - start < args.seconds:
            try:
                block = stream.queue.get(timeout=0.5)
            except Exception:
                continue
            block_count += 1
            p = peak_db(block)
            r = rms_db(block)
            p_s = f"{p:6.1f}" if math.isfinite(p) else "  -inf"
            r_s = f"{r:6.1f}" if math.isfinite(r) else "  -inf"
            # Simple text VU based on peak.
            bars_n = max(0, min(40, int((p - silent_floor) / 2))) if math.isfinite(p) else 0
            bars = "█" * bars_n + " " * (40 - bars_n)
            print(f"block {block_count:4d}  peak {p_s} dB  rms {r_s} dB  |{bars}|")
    finally:
        stream.stop()

    print(f"\nDone. blocks={block_count} dropped={stream.dropped_blocks}")
    if stream.dropped_blocks:
        print("WARNING: some blocks were dropped — consumer was too slow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
