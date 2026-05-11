"""Entry point. Parses CLI flags, runs a pre-flight check, then hands off to Textual.

The pre-flight surfaces missing system deps (sounddevice/PortAudio, no input
devices) as plain stderr messages so users see something actionable instead
of a TUI that flashes a stack trace and dies. Use --skip-preflight to bypass
when running headless tests.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from jack import __version__
from jack.config import Config

LOG_PATH = Path.home() / ".cache" / "jack" / "jack.log"


def _configure_logging(level_name: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        filename=str(LOG_PATH),
        filemode="a",
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _preflight() -> int:
    """Validate runtime prerequisites. Returns an exit code; 0 = OK."""
    try:
        import sounddevice as sd  # noqa: F401
    except ImportError as e:
        print(f"ERROR: sounddevice not importable ({e}).", file=sys.stderr)
        print("Try:  pip install -e .", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"ERROR: PortAudio not available: {e}", file=sys.stderr)
        print("Install with:  sudo pacman -S portaudio", file=sys.stderr)
        return 2

    from jack.audio.capture import list_input_devices
    try:
        devices = list_input_devices()
    except Exception as e:
        print(f"ERROR: could not enumerate audio devices: {e}", file=sys.stderr)
        return 2

    if not devices:
        print("WARNING: no input devices detected.", file=sys.stderr)
        print("Plug in the USB interface and check `arecord -l`.", file=sys.stderr)
        # Don't hard-fail — the setup screen will show the same message.
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="jack",
        description="TUI vinyl ripper: capture, auto-split, tag, and encode records to FLAC.",
    )
    p.add_argument("--version", action="version", version=f"jack {__version__}")
    p.add_argument(
        "--config",
        action="store_true",
        help="print the config file path and exit",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("JACK_LOG", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level (env: JACK_LOG; default INFO). Log file: ~/.cache/jack/jack.log",
    )
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="bypass audio-device pre-flight (useful for headless tests)",
    )
    p.add_argument(
        "--fixture",
        action="store_true",
        help="use bundled MB JSON fixtures (skips network calls). "
             "Equivalent to JACK_USE_FIXTURE=1",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.log_level)

    if args.config:
        print(Config.path())
        return 0

    if args.fixture:
        os.environ["JACK_USE_FIXTURE"] = "1"

    if not args.skip_preflight:
        rc = _preflight()
        if rc != 0:
            return rc

    # Defer Textual import so --version / --config / failing pre-flight don't
    # pay the cost of importing the whole UI stack.
    from jack.tui.app import JackApp

    JackApp(config=Config.load()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
