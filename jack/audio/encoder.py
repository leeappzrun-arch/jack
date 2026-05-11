"""Track buffering + FLAC encoding + Vorbis-comment tagging.

Two stages:

  1. ``TempWavWriter`` — opened the moment the silence detector emits
     ``track_start``. Each captured block is streamed via ``append()`` to a
     temp 24-bit WAV. Closed on ``track_end``. Memory stays flat regardless
     of side length.

  2. ``encode_to_flac()`` — reads the temp WAV (in a manageable chunk loop),
     writes a FLAC, applies Vorbis comments + embeds a front-cover picture
     block, then deletes the temp by default.

Output path layout:  ``{output_dir}/{Artist}/{Album}/{NN} - {Title}.flac``

Audio quality: vinyl rips at 24-bit FLAC are the practical archival standard.
libsndfile handles float32 → PCM_24 conversion internally on write.
"""
from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType
from platformdirs import user_cache_path

from jack.metadata.artwork import Artwork

logger = logging.getLogger(__name__)

# How much of the WAV we read at a time when converting to FLAC. Keeps RAM
# flat for arbitrarily long tracks (8 MB at 48 kHz/24-bit stereo).
ENCODE_CHUNK_FRAMES = 65536

_BAD_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str, fallback: str = "Unknown") -> str:
    """Clean a string for safe use as a path segment."""
    cleaned = _BAD_PATH_CHARS.sub("_", name or "").strip(" .")
    return cleaned or fallback


def temp_dir() -> Path:
    d = user_cache_path("jack", ensure_exists=True) / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Streaming WAV writer
# ---------------------------------------------------------------------------


class TempWavWriter:
    """Append float32 blocks to a 24-bit WAV temp file.

    Single-use: instantiate per track, close once. After ``close()`` the
    instance is exhausted; ``append`` will raise.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        directory: Path | None = None,
        prefix: str = "jack-track-",
    ) -> None:
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("sample_rate and channels must be positive")
        self.sample_rate = sample_rate
        self.channels = channels
        self.frames_written = 0

        directory = directory or temp_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile gives us a unique path without races.
        tf = tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".wav",
            prefix=prefix,
            dir=str(directory),
            delete=False,
        )
        tf.close()
        self.path = Path(tf.name)

        self._sf: sf.SoundFile | None = sf.SoundFile(
            str(self.path),
            mode="w",
            samplerate=sample_rate,
            channels=channels,
            subtype="PCM_24",
            format="WAV",
        )

    @property
    def closed(self) -> bool:
        return self._sf is None

    @property
    def duration_s(self) -> float:
        return self.frames_written / self.sample_rate

    def append(self, block: np.ndarray) -> None:
        if self._sf is None:
            raise RuntimeError("TempWavWriter is closed")
        if block.ndim != 2 or block.shape[1] != self.channels:
            raise ValueError(
                f"expected (frames, {self.channels}) block, got shape {block.shape}"
            )
        self._sf.write(block)
        self.frames_written += block.shape[0]

    def close(self) -> tuple[Path, int]:
        if self._sf is None:
            return self.path, self.frames_written
        try:
            self._sf.close()
        finally:
            self._sf = None
        return self.path, self.frames_written

    def abort(self) -> None:
        """Close + delete the temp file. Use when discarding a too-short track."""
        if self._sf is not None:
            try:
                self._sf.close()
            finally:
                self._sf = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.exception("failed to delete aborted temp WAV %s", self.path)

    def __enter__(self) -> "TempWavWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.abort()
        else:
            if not self.closed:
                self.close()


# ---------------------------------------------------------------------------
# Track metadata + paths
# ---------------------------------------------------------------------------


@dataclass
class TrackMetadata:
    artist: str
    album: str
    title: str
    tracknumber: int
    totaltracks: int | None = None
    discnumber: int = 1
    totaldiscs: int | None = None
    date: str = ""           # release year, e.g. "1973"
    albumartist: str | None = None  # defaults to artist if None


def output_path_for(
    metadata: TrackMetadata,
    *,
    output_dir: Path,
) -> Path:
    """Compute the final FLAC path. Does not create parent dirs."""
    artist_dir = _sanitize(metadata.albumartist or metadata.artist, "Unknown Artist")
    album_dir = _sanitize(metadata.album, "Unknown Album")
    title_part = _sanitize(metadata.title, f"Track {metadata.tracknumber}")
    filename = f"{metadata.tracknumber:02d} - {title_part}.flac"
    return output_dir / artist_dir / album_dir / filename


# ---------------------------------------------------------------------------
# FLAC encode + tag + embed art
# ---------------------------------------------------------------------------


def encode_to_flac(
    wav_path: Path,
    output_path: Path,
    metadata: TrackMetadata,
    *,
    artwork: Artwork | None = None,
    delete_source: bool = True,
    overwrite: bool = True,
) -> Path:
    """Convert a temp WAV to a tagged FLAC. Returns the final path."""
    if not wav_path.exists():
        raise FileNotFoundError(f"source WAV missing: {wav_path}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    _wav_to_flac(wav_path, output_path)
    _apply_tags(output_path, metadata)
    if artwork is not None:
        _embed_picture(output_path, artwork)

    if delete_source:
        try:
            wav_path.unlink()
        except OSError:
            logger.warning("failed to delete temp WAV %s", wav_path)

    return output_path


def _wav_to_flac(wav_path: Path, flac_path: Path) -> None:
    """Stream WAV → FLAC at PCM_24. Keeps RAM flat."""
    with sf.SoundFile(str(wav_path), mode="r") as src:
        with sf.SoundFile(
            str(flac_path),
            mode="w",
            samplerate=src.samplerate,
            channels=src.channels,
            subtype="PCM_24",
            format="FLAC",
        ) as dst:
            while True:
                block = src.read(ENCODE_CHUNK_FRAMES, dtype="float32", always_2d=True)
                if block.shape[0] == 0:
                    break
                dst.write(block)


def _apply_tags(flac_path: Path, m: TrackMetadata) -> None:
    audio = FLAC(str(flac_path))
    audio["TITLE"] = m.title
    audio["ARTIST"] = m.artist
    audio["ALBUM"] = m.album
    audio["ALBUMARTIST"] = m.albumartist or m.artist
    audio["TRACKNUMBER"] = str(m.tracknumber)
    if m.totaltracks is not None:
        audio["TOTALTRACKS"] = str(m.totaltracks)
        audio["TRACKTOTAL"] = str(m.totaltracks)  # alt spelling, widely supported
    audio["DISCNUMBER"] = str(m.discnumber)
    if m.totaldiscs is not None:
        audio["TOTALDISCS"] = str(m.totaldiscs)
        audio["DISCTOTAL"] = str(m.totaldiscs)
    if m.date:
        audio["DATE"] = m.date
    audio.save()


def _embed_picture(flac_path: Path, art: Artwork) -> None:
    audio = FLAC(str(flac_path))
    audio.clear_pictures()  # avoid stacking on re-encode
    pic = Picture()
    pic.type = PictureType.COVER_FRONT  # 3
    pic.mime = art.mime_type
    pic.desc = "Cover (front)"
    if art.width:
        pic.width = art.width
    if art.height:
        pic.height = art.height
    pic.data = art.image_path.read_bytes()
    audio.add_picture(pic)
    audio.save()


# ---------------------------------------------------------------------------
# Convenience: full per-track pipeline
# ---------------------------------------------------------------------------


def encode_blocks(
    blocks: Iterable[np.ndarray],
    *,
    sample_rate: int,
    channels: int,
    metadata: TrackMetadata,
    output_dir: Path,
    artwork: Artwork | None = None,
) -> Path:
    """Write `blocks` to a temp WAV, then encode and tag a FLAC.

    Convenience for tests and one-shot uses; the live ripper drives
    TempWavWriter and encode_to_flac separately so the encode can happen
    on a worker thread while the next track is already recording.
    """
    writer = TempWavWriter(sample_rate=sample_rate, channels=channels)
    try:
        for b in blocks:
            writer.append(b)
        wav_path, _frames = writer.close()
    except Exception:
        writer.abort()
        raise
    return encode_to_flac(
        wav_path,
        output_path_for(metadata, output_dir=output_dir),
        metadata,
        artwork=artwork,
    )
