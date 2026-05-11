"""Step-5 smoke test: fetch and/or render album artwork.

    # Render an image file directly with chafa
    python scripts/artwork_test.py path/to/cover.jpg

    # Fetch from Cover Art Archive by MBID, then render
    python scripts/artwork_test.py --mbid a1ad30cb-b8c4-4d68-9253-15b18fcde1d7

    # Show the fallback ASCII art
    python scripts/artwork_test.py --fallback
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jack.metadata import artwork


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", type=Path, help="image file to render")
    ap.add_argument("--mbid", help="fetch front cover from Cover Art Archive")
    ap.add_argument("--size", default="40x20", help="WIDTHxHEIGHT (cells)")
    ap.add_argument("--colors", type=int, default=256)
    ap.add_argument("--fallback", action="store_true", help="show ASCII fallback")
    ap.add_argument("--force", action="store_true", help="ignore cache when fetching")
    args = ap.parse_args()

    if args.fallback:
        print(artwork.fallback_art())
        return 0

    image_path: Path | None = None
    if args.mbid:
        print(f"Fetching CAA front cover for {args.mbid}...")
        try:
            art = artwork.fetch_front_cover(args.mbid, force=args.force)
        except artwork.ArtworkError as e:
            print(f"fetch failed: {e}")
            return 1
        if art is None:
            print("No artwork available for this release.")
            print(artwork.fallback_art())
            return 0
        print(f"Got {art.mime_type} {art.width}x{art.height} -> {art.image_path}")
        image_path = art.image_path
    elif args.image:
        image_path = args.image
    else:
        ap.error("provide an image path, --mbid, or --fallback")

    if not artwork.chafa_available():
        print("chafa not installed (sudo pacman -S chafa). Showing fallback:")
        print(artwork.fallback_art())
        return 0

    try:
        w, h = (int(x) for x in args.size.lower().split("x"))
    except ValueError:
        ap.error("--size must look like 40x20")
        return 2

    try:
        rendered = artwork.render_with_chafa(image_path, width=w, height=h, colors=args.colors)
    except artwork.ArtworkError as e:
        print(f"render failed: {e}")
        return 1

    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
