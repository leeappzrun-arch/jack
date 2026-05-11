"""Step-3 smoke tests for the silence detector.

Two modes:

    python scripts/silence_test.py synthetic
        Feed a synthetic signal (silence/tone/silence/tone/silence) and assert
        the detector identifies the correct number of tracks. Exits non-zero
        on mismatch.

    python scripts/silence_test.py live [DEVICE_INDEX]
        Stream from the USB device and print split events live. Useful for
        tuning the threshold against your room's noise floor. Ctrl-C to stop.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

# Allow running from a clean checkout without `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from jack.audio.silence import DetectorEvent, SilenceDetector, block_rms_db, feed_all

BLOCKSIZE = 1024


def make_block(frames: int, channels: int, amp: float, freq: float, sr: int, phase: float) -> np.ndarray:
    """Sine block scaled to `amp` (linear, full-scale = 1.0). Returns (frames, ch) float32."""
    t = (np.arange(frames) + phase) / sr
    sig = amp * np.sin(2 * np.pi * freq * t).astype(np.float32)
    return np.stack([sig] * channels, axis=1)


def make_silence_block(frames: int, channels: int, noise_amp: float = 0.0) -> np.ndarray:
    """Silence with optional shaped noise to mimic vinyl surface hiss."""
    if noise_amp <= 0:
        return np.zeros((frames, channels), dtype=np.float32)
    return (np.random.standard_normal((frames, channels)) * noise_amp).astype(np.float32)


def synth_blocks(spec, sr: int, channels: int = 2, blocksize: int = BLOCKSIZE):
    """spec is a list of (kind, seconds, params). Yields blocks."""
    phase = 0
    for kind, secs, params in spec:
        total_frames = int(secs * sr)
        remaining = total_frames
        while remaining > 0:
            n = min(blocksize, remaining)
            if kind == "tone":
                yield make_block(n, channels, params["amp"], params["freq"], sr, phase)
                phase += n
            else:  # silence (optionally noisy)
                yield make_silence_block(n, channels, params.get("noise", 0.0))
                phase = 0
            remaining -= n


def run_synthetic() -> int:
    sr = 48000
    spec = [
        ("silence", 3.0, {"noise": 0.0008}),       # leading silence + surface hiss
        ("tone",    25.0, {"amp": 0.3, "freq": 440}),   # track 1
        ("silence", 3.0, {"noise": 0.0008}),       # inter-track gap
        ("tone",    25.0, {"amp": 0.3, "freq": 660}),   # track 2
        ("silence", 3.0, {"noise": 0.0008}),       # inter-track gap
        ("tone",    25.0, {"amp": 0.3, "freq": 880}),   # track 3
        ("silence", 4.0, {"noise": 0.0008}),       # trailing silence
    ]
    expected_tracks = sum(1 for k, *_ in spec if k == "tone")

    detector = SilenceDetector(
        sample_rate=sr,
        threshold_db=-52.0,
        silence_duration_s=2.5,
        min_track_duration_s=20.0,
    )

    audio_blocks_per_track: list[int] = []
    current_count = 0
    track_open = False
    starts = ends = 0

    events = feed_all(detector, synth_blocks(spec, sr=sr))
    for ev in events:
        if ev.kind == "track_start":
            starts += 1
            track_open = True
            current_count = 0
        elif ev.kind == "track_end":
            ends += 1
            track_open = False
            audio_blocks_per_track.append(current_count)
        elif ev.kind == "audio":
            current_count += 1
    if track_open:
        audio_blocks_per_track.append(current_count)

    print(f"expected tracks: {expected_tracks}")
    print(f"track_start events: {starts}")
    print(f"track_end events:   {ends}")
    print(f"blocks per track:   {audio_blocks_per_track}")

    ok = starts == expected_tracks == ends
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def run_live(device_index: int | None, seconds: float, threshold_db: float) -> int:
    # Imported here so `synthetic` mode doesn't require sounddevice/PortAudio.
    from jack.audio.capture import CaptureStream, list_usb_input_devices

    if device_index is None:
        usb = list_usb_input_devices()
        if not usb:
            print("No USB input devices found.")
            return 1
        device_index = usb[0].index
        print(f"Auto-picked USB device [{device_index}] {usb[0].name}")

    stream = CaptureStream(device_index, sample_rate=None, channels=2)
    stream.start()
    print(f"Live: {stream.sample_rate} Hz, threshold {threshold_db} dB. Ctrl-C to stop.")

    detector = SilenceDetector(
        sample_rate=stream.sample_rate,
        threshold_db=threshold_db,
        silence_duration_s=2.5,
        min_track_duration_s=20.0,
    )

    track_n = 0
    started_at: float | None = None
    block_count = 0
    last_print = 0.0
    deadline = time.monotonic() + seconds if seconds > 0 else math.inf
    try:
        while time.monotonic() < deadline:
            try:
                block = stream.queue.get(timeout=0.5)
            except Exception:
                continue
            block_count += 1
            for ev in detector.feed(block):
                if ev.kind == "track_start":
                    track_n += 1
                    started_at = time.monotonic()
                    print(f"\n>>> TRACK {track_n} START (block {block_count})")
                elif ev.kind == "track_end":
                    dur = time.monotonic() - started_at if started_at else 0.0
                    print(f"\n<<< TRACK {track_n} END   (elapsed {dur:.1f}s, block {block_count})")
                    started_at = None
            # Periodic VU heartbeat
            now = time.monotonic()
            if now - last_print > 0.25:
                db = block_rms_db(block)
                db_s = f"{db:6.1f}" if math.isfinite(db) else "  -inf"
                bars = max(0, min(40, int((db + 90) / 2))) if math.isfinite(db) else 0
                print(f"\rstate={detector.state.value:<17} rms {db_s} dB |"
                      f"{'█' * bars}{' ' * (40 - bars)}|", end="", flush=True)
                last_print = now
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        for ev in detector.finalize():
            if ev.kind == "track_end":
                print(f"\n<<< TRACK {track_n} END (stream stop)")
        print(f"\nfinal: tracks={track_n} blocks={block_count} dropped={stream.dropped_blocks}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("synthetic")
    live = sub.add_parser("live")
    live.add_argument("device", nargs="?", type=int)
    live.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl-C")
    live.add_argument("--threshold", type=float, default=-52.0)
    args = ap.parse_args()

    if args.mode == "synthetic":
        return run_synthetic()
    return run_live(args.device, args.seconds, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
