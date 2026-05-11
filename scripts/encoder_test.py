"""Step-6 smoke test: encode a synthesized track to FLAC, verify by readback.

    python scripts/encoder_test.py [OUTDIR]

Writes to /tmp/jack-encoder-test by default. Verifies:
- TempWavWriter streams blocks without loading everything into RAM
- FLAC file exists, has expected length and sample rate
- Vorbis tags are present and correct
- Artwork is embedded when supplied
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from jack.audio.encoder import (
    TempWavWriter,
    TrackMetadata,
    encode_to_flac,
    output_path_for,
)
from jack.metadata.artwork import Artwork


def make_tone_block(frames: int, channels: int, freq: float, sr: int, phase: int) -> np.ndarray:
    t = (np.arange(frames) + phase) / sr
    sig = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([sig] * channels, axis=1)


def make_test_image(path: Path) -> None:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (300, 300), color=(15, 20, 40))
    d = ImageDraw.Draw(img)
    d.ellipse((50, 50, 250, 250), outline=(220, 220, 220), width=4)
    d.ellipse((130, 130, 170, 170), fill=(80, 0, 0))
    img.save(path, "JPEG", quality=92)


def main() -> int:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/jack-encoder-test")
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    sr = 48000
    channels = 2
    duration_s = 3.0
    blocksize = 1024
    total_frames = int(duration_s * sr)

    print(f"Output dir: {outdir}")

    # --- Stage 1: stream synthesized audio through TempWavWriter ----------
    writer = TempWavWriter(sample_rate=sr, channels=channels)
    print(f"Temp WAV: {writer.path}")
    phase = 0
    while phase < total_frames:
        n = min(blocksize, total_frames - phase)
        writer.append(make_tone_block(n, channels, 440.0, sr, phase))
        phase += n
    wav_path, frames = writer.close()
    print(f"  wrote {frames} frames ({frames / sr:.3f}s)")
    assert frames == total_frames, f"frames {frames} != {total_frames}"

    # --- Stage 2: prepare artwork -----------------------------------------
    art_path = Path("/tmp/jack-encoder-cover.jpg")
    make_test_image(art_path)
    art = Artwork(image_path=art_path, mime_type="image/jpeg", width=300, height=300)

    # --- Stage 3: tags + FLAC encode --------------------------------------
    metadata = TrackMetadata(
        artist="Test Artist / With/Slash",
        album='Album: With "Bad" Chars?',
        title="Track One — Hello",
        tracknumber=1,
        totaltracks=10,
        discnumber=1,
        totaldiscs=1,
        date="1973",
        albumartist="Test Artist",
    )
    target = output_path_for(metadata, output_dir=outdir)
    print(f"Target FLAC: {target}")

    final = encode_to_flac(wav_path, target, metadata, artwork=art, delete_source=True)
    assert final == target
    assert final.exists(), "FLAC was not written"
    assert not wav_path.exists(), "temp WAV should be deleted"
    print(f"  wrote {final.stat().st_size:,} bytes")

    # --- Stage 4: verify -------------------------------------------------
    info = sf.info(str(final))
    print(f"\nFLAC info: format={info.format} subtype={info.subtype} sr={info.samplerate} "
          f"ch={info.channels} frames={info.frames} dur={info.duration:.3f}s")
    assert info.samplerate == sr
    assert info.channels == channels
    assert abs(info.duration - duration_s) < 0.01
    assert info.subtype == "PCM_24"

    audio = FLAC(str(final))
    print("\nTags:")
    for k, v in sorted(audio.tags or []):
        print(f"  {k} = {v}")
    assert audio["TITLE"] == ["Track One — Hello"]
    assert audio["ARTIST"] == ["Test Artist / With/Slash"]
    assert audio["ALBUM"] == ['Album: With "Bad" Chars?']
    assert audio["TRACKNUMBER"] == ["1"]
    assert audio["TOTALTRACKS"] == ["10"]
    assert audio["DATE"] == ["1973"]
    assert audio["DISCNUMBER"] == ["1"]
    assert audio["ALBUMARTIST"] == ["Test Artist"]

    pics = audio.pictures
    assert len(pics) == 1, f"expected 1 embedded picture, got {len(pics)}"
    p = pics[0]
    print(f"\nEmbedded art: type={p.type} mime={p.mime} {p.width}x{p.height} "
          f"({len(p.data):,} bytes)")
    assert p.mime == "image/jpeg"
    assert p.type == 3  # COVER_FRONT
    assert len(p.data) > 0

    # Path sanitisation: slashes / colons / quotes should be replaced
    rel = final.relative_to(outdir)
    parts = rel.parts
    assert "/" not in parts[0] and "\\" not in parts[0]
    assert '"' not in parts[1] and ":" not in parts[1] and "?" not in parts[1]
    print(f"\nSanitized path parts: {parts}")

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
