"""Step-4 smoke test: search MusicBrainz and fetch a release tracklist.

    python scripts/mb_test.py "Pink Floyd" "The Dark Side of the Moon"
    python scripts/mb_test.py "Pink Floyd" "The Dark Side of the Moon" --pick 0
    python scripts/mb_test.py --mbid <release-mbid>

Requires network access. Uses a default contact email — pass --contact to
override (MB asks for one in the User-Agent for support purposes).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jack.config import Config
from jack.metadata import musicbrainz as mbz

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "  ? "
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def print_candidates(candidates):
    print(f"\n{len(candidates)} candidates (sorted: vinyl first, then score):\n")
    for i, c in enumerate(candidates):
        fmt = c.format or "?"
        print(
            f"  [{i}] score={c.score:3d}  {c.artist} — {c.title}\n"
            f"        {c.date or '????'}  {c.country or '??'}  {fmt}  "
            f"({c.track_count} tracks)  mbid={c.mbid}"
        )


def print_release(details: mbz.ReleaseDetails):
    print(f"\nRelease: {details.artist} — {details.album} ({details.date})")
    print(f"Format:  {details.format or '?'}")
    print(f"Sides:   {details.sides or 'n/a'}  side_a_count={details.side_a_count}")
    print(f"\n{'#':>2}  {'num':<4} {'dur':>6}  {'side':>4}  title")
    print("-" * 70)
    for t in details.tracks:
        print(f"  {t.position:>2}  {t.number:<4} {fmt_ms(t.duration_ms):>6}  {t.side or '-':>4}  {t.title}")


def run_fixtures() -> int:
    """Parse local JSON fixtures through the real parsing code paths."""
    search_json = json.loads((FIXTURE_DIR / "mb_search_pink_floyd.json").read_text())
    release_json = json.loads((FIXTURE_DIR / "mb_release_dsotm_vinyl.json").read_text())

    print("=== parse_search_results (vinyl-preferred) ===")
    candidates = mbz.parse_search_results(search_json, limit=5, prefer_vinyl=True)
    print_candidates(candidates)

    print("\n=== parse_release (Pink Floyd DSOTM Vinyl) ===")
    details = mbz.parse_release(release_json)
    print_release(details)

    print("\n=== to_app_tracks (uses derived side_a_count) ===")
    tracks = mbz.to_app_tracks(details)
    for t in tracks:
        ms = t.duration_ms or 0
        print(f"  {t.number:>2}. side={t.side.value}  {ms // 1000 // 60:02d}:{ms // 1000 % 60:02d}  {t.title}")

    # Sanity checks
    assert candidates, "no candidates parsed"
    assert candidates[0].format and "vinyl" in candidates[0].format.lower(), "vinyl not first"
    assert details.side_a_count == 5, f"expected side_a_count=5, got {details.side_a_count}"
    assert len(tracks) == 10
    assert tracks[4].side.value == "A" and tracks[5].side.value == "B"
    print("\nfixture assertions: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artist", nargs="?", default="")
    ap.add_argument("album", nargs="?", default="")
    ap.add_argument("--mbid", help="skip search; fetch this release directly")
    ap.add_argument("--pick", type=int, help="auto-pick this candidate index and fetch it")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--contact", default=None, help="override MB User-Agent contact")
    ap.add_argument(
        "--fixture",
        action="store_true",
        help="parse local fixture JSON instead of hitting the API",
    )
    args = ap.parse_args()

    if args.fixture:
        return run_fixtures()

    cfg = Config.load()
    contact = args.contact or cfg.musicbrainz_contact
    mbz.configure(contact)

    if args.mbid:
        details = mbz.get_release(args.mbid)
        print_release(details)
        return 0

    if not args.artist and not args.album:
        ap.error("provide artist + album (or --mbid)")

    candidates = mbz.search_releases(args.artist, args.album, limit=args.limit)
    if not candidates:
        print("No releases found.")
        return 1
    print_candidates(candidates)

    if args.pick is not None:
        if not (0 <= args.pick < len(candidates)):
            print(f"--pick {args.pick} out of range")
            return 2
        details = mbz.get_release(candidates[args.pick].mbid)
        print_release(details)
    return 0


if __name__ == "__main__":
    sys.exit(main())
