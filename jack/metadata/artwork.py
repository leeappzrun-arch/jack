"""Album artwork: Cover Art Archive fetch + chafa rendering.

Two distinct uses for the cover image:

1. **Embed in FLAC** — we want a reasonable-resolution JPEG/PNG (CAA's
   `front-1200` thumb is plenty; full-size originals can be 5 MB+).
2. **Display in TUI** — render to ANSI block art with `chafa`, captured as
   a string that a Textual `Static` widget can show.

Downloads are cached under platformdirs' user cache dir, keyed by MBID, so
repeated runs don't re-hit CAA. CAA URL format is well-documented and stable:
    https://coverartarchive.org/release/{mbid}/front-1200
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests
from platformdirs import user_cache_path

from jack import __version__

logger = logging.getLogger(__name__)

CAA_BASE = "https://coverartarchive.org"
DEFAULT_TIMEOUT = 20  # seconds
USER_AGENT = f"jack/{__version__} (+https://github.com/local/jack)"


@dataclass
class Artwork:
    image_path: Path
    mime_type: str  # "image/jpeg" / "image/png"
    width: int | None = None
    height: int | None = None


class ArtworkError(RuntimeError):
    pass


def cache_dir() -> Path:
    d = user_cache_path("jack", ensure_exists=True) / "artwork"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Cover Art Archive
# ---------------------------------------------------------------------------


def fetch_front_cover(
    mbid: str,
    *,
    size: int | None = 1200,
    force: bool = False,
    session: requests.Session | None = None,
) -> Artwork | None:
    """Download the front cover for an MB release.

    Returns None if CAA has no artwork for the release (404). Raises
    ArtworkError on other HTTP / network failures.

    `size`: CAA serves canonical thumbnails at 250/500/1200 px. Pass None
    for the original full-resolution file (can be large).
    """
    if not mbid:
        raise ArtworkError("MBID required")

    # Use a stable cache filename. We don't know the content type until after
    # the request, so probe cache for either extension.
    base = cache_dir() / f"{mbid}-{size or 'orig'}"
    for ext, mime in ((".jpg", "image/jpeg"), (".png", "image/png")):
        p = base.with_suffix(ext)
        if p.exists() and not force:
            logger.debug("artwork cache hit: %s", p)
            return Artwork(image_path=p, mime_type=mime, **_image_dims(p))

    url = f"{CAA_BASE}/release/{mbid}/front"
    if size:
        url += f"-{size}"

    sess = session or requests.Session()
    try:
        resp = sess.get(
            url,
            allow_redirects=True,
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        )
    except requests.RequestException as e:
        raise ArtworkError(f"network error fetching cover: {e}") from e

    if resp.status_code == 404:
        logger.info("no artwork on CAA for %s", mbid)
        return None
    if resp.status_code >= 400:
        raise ArtworkError(f"CAA returned HTTP {resp.status_code} for {url}")

    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type == "image/png":
        ext, mime = ".png", "image/png"
    else:
        # CAA serves JPEG for almost everything; default to that.
        ext, mime = ".jpg", "image/jpeg"

    out_path = base.with_suffix(ext)
    with out_path.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                fh.write(chunk)

    dims = _image_dims(out_path)
    return Artwork(image_path=out_path, mime_type=mime, **dims)


def _image_dims(path: Path) -> dict:
    try:
        from PIL import Image
    except ImportError:
        return {"width": None, "height": None}
    try:
        with Image.open(path) as img:
            return {"width": img.width, "height": img.height}
    except Exception:
        return {"width": None, "height": None}


# ---------------------------------------------------------------------------
# chafa rendering
# ---------------------------------------------------------------------------


def chafa_available() -> bool:
    return shutil.which("chafa") is not None


def render_with_chafa(
    image_path: Path,
    *,
    width: int = 20,
    height: int = 20,
    colors: int = 256,
) -> str:
    """Render an image to ANSI block art. Returns chafa's stdout verbatim.

    Caller drops this into a `Static` widget with `markup=False`. Raises
    ArtworkError if chafa isn't installed or the render fails.
    """
    if not chafa_available():
        raise ArtworkError("chafa is not installed (pacman -S chafa).")
    if not image_path.exists():
        raise ArtworkError(f"image not found: {image_path}")

    cmd = [
        "chafa",
        f"--size={width}x{height}",
        f"--colors={colors}",
        "--symbols=block",
        "--format=symbols",
        "--polite=on",  # do not emit cursor-restore / mouse-mode escapes
        str(image_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError as e:
        raise ArtworkError(f"chafa not callable: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise ArtworkError(f"chafa timed out: {e}") from e

    if result.returncode != 0:
        raise ArtworkError(
            f"chafa exited {result.returncode}: {result.stderr.strip() or 'no stderr'}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


_VINYL_FALLBACK = r"""
       _.--""--._
      /  .-""-.  \
     /  /      \  \
    |  |  .-.   |  |
    |  | (   )  |  |
    |  |  '-'   |  |
     \  \      /  /
      \  '-..-'  /
       '-.____.-'
"""


def fallback_art() -> str:
    """ASCII fallback when no cover is available or chafa isn't installed."""
    return _VINYL_FALLBACK
