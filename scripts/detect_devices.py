"""Step-1 smoke test: confirm sounddevice can see the USB audio interface.

Run with the project venv active:
    python scripts/detect_devices.py
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        import sounddevice as sd
    except ImportError as e:
        print(f"ERROR: sounddevice not importable ({e}).")
        print("Install deps:  pip install -e .")
        return 2
    except OSError as e:
        # PortAudio shared library missing / not loadable.
        print(f"ERROR: PortAudio not available: {e}")
        print("Install with:  sudo pacman -S portaudio")
        return 2

    print(f"sounddevice version: {sd.__version__}")
    print(f"PortAudio version:   {sd.get_portaudio_version()[1]}\n")

    devices = sd.query_devices()
    if not devices:
        print("No audio devices found.")
        return 1

    print(f"{'idx':>3}  {'in':>2}  {'out':>3}  {'sr':>6}  name")
    print("-" * 70)
    usb_inputs: list[tuple[int, str]] = []
    for idx, dev in enumerate(devices):
        name = dev["name"]
        ins = dev["max_input_channels"]
        outs = dev["max_output_channels"]
        sr = int(dev["default_samplerate"])
        print(f"{idx:>3}  {ins:>2}  {outs:>3}  {sr:>6}  {name}")
        if ins > 0 and "usb" in name.lower():
            usb_inputs.append((idx, name))

    print()
    if usb_inputs:
        print("USB input device(s) detected:")
        for idx, name in usb_inputs:
            print(f"  [{idx}] {name}")
    else:
        print("No USB input devices detected.")
        print("(Plug in the interface and re-run, or check `arecord -l`.)")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
