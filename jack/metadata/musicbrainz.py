"""MusicBrainz release lookup.

We hit two endpoints:

  1. `search_releases(artist, release)` — returns candidate releases ranked by
     MB's search score. We surface the top N for the user to pick from.
  2. `get_release_by_id(mbid, includes=[recordings, media, ...])` — fetches the
     full tracklist with durations.

MB requires a User-Agent and enforces 1 req/sec. `musicbrainzngs` handles the
rate limiting internally once `set_rate_limit()` is called; we just need to
remember to call `configure()` before any API call.

Vinyl-specific notes:
- On vinyl releases, MB stores track numbers as "A1", "A2", "B1", etc. (the
  `number` field on each track). We parse the leading letter as the side and
  use that to derive `side_a_count`. CD-style numbering ("1", "2", "3") leaves
  side info empty — the setup screen will ask the user to mark the split.
- `length` is the recording's duration in ms, returned as a string. Some
  recordings have no length (e.g. live data missing) — we map those to None
  and the silence-detector's track count is the source of truth in that case.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import musicbrainzngs as mb

from jack import __version__
from jack.state import Side, Track, TrackStatus

logger = logging.getLogger(__name__)

_configured = False

# Track-number prefixes used on MB for vinyl releases. Anything beyond D is
# rare but valid (triple LPs etc.).
_SIDE_PREFIX_RE = re.compile(r"^\s*([A-Z])\s*\d+", re.IGNORECASE)


class MusicBrainzError(RuntimeError):
    """Wraps musicbrainzngs errors with a friendlier message."""


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def configure(contact: str) -> None:
    """Set the User-Agent and rate limit. Idempotent."""
    global _configured
    mb.set_useragent("jack", __version__, contact)
    mb.set_rate_limit(limit_or_interval=1.0, new_requests=1)  # 1 req/sec
    _configured = True


def _require_configured() -> None:
    if not _configured:
        raise MusicBrainzError(
            "MusicBrainz not configured — call musicbrainz.configure(contact) first."
        )


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ReleaseCandidate:
    mbid: str
    artist: str
    title: str
    date: str  # "YYYY" or "YYYY-MM-DD"; "" if unknown
    country: str | None
    format: str | None  # e.g. "Vinyl", "12\" Vinyl", "CD"
    track_count: int
    score: int  # MB search score, 0–100


@dataclass
class MBTrack:
    position: int       # 1-based across the entire release
    number: str         # original MB number string, e.g. "A1", "1"
    title: str
    duration_ms: int | None
    side: str | None    # "A" / "B" / ... parsed from `number`, or None


@dataclass
class ReleaseDetails:
    mbid: str
    artist: str
    album: str
    date: str
    format: str | None
    tracks: list[MBTrack] = field(default_factory=list)
    # Number of tracks on side A. None means we couldn't derive it.
    side_a_count: int | None = None
    # Earliest side letter seen (usually "A"). Used when computing side B start.
    sides: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _format_from_media(media: list[dict[str, Any]] | None) -> str | None:
    if not media:
        return None
    formats = [m.get("format") for m in media if m.get("format")]
    return " + ".join(formats) if formats else None


def search_releases(
    artist: str,
    album: str,
    *,
    limit: int = 5,
    prefer_vinyl: bool = True,
) -> list[ReleaseCandidate]:
    """Search MB for an artist+release. Returns up to `limit` candidates.

    When `prefer_vinyl=True`, vinyl releases are sorted to the top — useful
    since the same album often has many MB entries across formats.
    """
    _require_configured()
    if not artist.strip() and not album.strip():
        raise MusicBrainzError("Must provide artist or album to search.")
    try:
        result = mb.search_releases(
            artist=artist or None,
            release=album or None,
            limit=limit * 4,  # over-fetch so prefer_vinyl has options to reorder
        )
    except mb.WebServiceError as e:
        raise MusicBrainzError(f"MusicBrainz search failed: {e}") from e

    return parse_search_results(result, limit=limit, prefer_vinyl=prefer_vinyl)


def parse_search_results(
    result: dict[str, Any],
    *,
    limit: int = 5,
    prefer_vinyl: bool = True,
) -> list[ReleaseCandidate]:
    """Convert a raw MB `search_releases` response into ReleaseCandidates."""
    raw = result.get("release-list", [])
    out: list[ReleaseCandidate] = []
    for r in raw:
        credit = r.get("artist-credit-phrase") or _join_artist_credit(r.get("artist-credit"))
        media = r.get("medium-list") or []
        track_count = sum(int(m.get("track-count", 0) or 0) for m in media)
        out.append(
            ReleaseCandidate(
                mbid=r["id"],
                artist=credit or "",
                title=r.get("title", ""),
                date=r.get("date", ""),
                country=r.get("country"),
                format=_format_from_media(media),
                track_count=track_count,
                score=int(r.get("ext:score", 0) or 0),
            )
        )

    if prefer_vinyl:
        out.sort(key=lambda c: (_vinyl_rank(c.format), -c.score))
    else:
        out.sort(key=lambda c: -c.score)
    return out[:limit]


def _vinyl_rank(fmt: str | None) -> int:
    """Lower = better. Vinyl first, then anything else, unknowns last."""
    if not fmt:
        return 2
    f = fmt.lower()
    if "vinyl" in f or "lp" in f:
        return 0
    return 1


def _join_artist_credit(credit: Any) -> str:
    """MB artist-credit can be a list of dicts and join strings."""
    if not credit:
        return ""
    if isinstance(credit, str):
        return credit
    parts: list[str] = []
    for item in credit:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            artist = item.get("artist") or {}
            name = item.get("name") or artist.get("name") or ""
            join = item.get("joinphrase") or ""
            parts.append(name + join)
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Release detail fetch
# ---------------------------------------------------------------------------


def get_release(mbid: str) -> ReleaseDetails:
    """Fetch the full tracklist for a release."""
    _require_configured()
    try:
        result = mb.get_release_by_id(
            mbid,
            includes=["recordings", "media", "artist-credits"],
        )
    except mb.WebServiceError as e:
        raise MusicBrainzError(f"MusicBrainz release fetch failed: {e}") from e
    return parse_release(result)


def parse_release(result: dict[str, Any]) -> ReleaseDetails:
    """Convert a raw MB `get_release_by_id` response into ReleaseDetails."""
    release = result["release"]
    mbid = release["id"]
    artist = release.get("artist-credit-phrase") or _join_artist_credit(release.get("artist-credit"))
    album = release.get("title", "")
    date = release.get("date", "")
    media = release.get("medium-list", []) or []
    fmt = _format_from_media(media)

    tracks: list[MBTrack] = []
    sides_seen: list[str] = []
    position = 0
    for medium in media:
        for t in medium.get("track-list", []) or []:
            position += 1
            number = (t.get("number") or "").strip()
            side = _parse_side(number)
            if side and side not in sides_seen:
                sides_seen.append(side)
            recording = t.get("recording") or {}
            title = recording.get("title") or t.get("title") or ""
            length = t.get("length") or recording.get("length")
            duration_ms = int(length) if length and str(length).isdigit() else None
            tracks.append(
                MBTrack(
                    position=position,
                    number=number,
                    title=title,
                    duration_ms=duration_ms,
                    side=side,
                )
            )

    side_a_count = _derive_side_a_count(tracks, sides_seen)
    return ReleaseDetails(
        mbid=mbid,
        artist=artist,
        album=album,
        date=date,
        format=fmt,
        tracks=tracks,
        side_a_count=side_a_count,
        sides=sides_seen,
    )


def _parse_side(number: str) -> str | None:
    m = _SIDE_PREFIX_RE.match(number)
    return m.group(1).upper() if m else None


def _derive_side_a_count(tracks: list[MBTrack], sides: list[str]) -> int | None:
    """If every track has a side letter, return the count for the first side."""
    if not tracks or not sides:
        return None
    if any(t.side is None for t in tracks):
        return None
    first_side = sides[0]
    return sum(1 for t in tracks if t.side == first_side)


# ---------------------------------------------------------------------------
# Adapter to app state
# ---------------------------------------------------------------------------


def to_app_tracks(details: ReleaseDetails, side_a_count: int | None = None) -> list[Track]:
    """Convert MB tracks to `jack.state.Track` objects.

    `side_a_count` overrides the derived value (used when the user pins the
    split manually via the setup screen). If still unknown, all tracks are
    placed on Side A — the UI will warn and prompt during the flip step.
    """
    cutoff = side_a_count if side_a_count is not None else details.side_a_count
    out: list[Track] = []
    for t in details.tracks:
        if cutoff is not None and t.position > cutoff:
            side = Side.B
        else:
            side = Side.A
        out.append(
            Track(
                number=t.position,
                title=t.title,
                duration_ms=t.duration_ms,
                side=side,
                status=TrackStatus.WAITING,
            )
        )
    return out
