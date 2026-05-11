"""User configuration: device choice, thresholds, output directory.

Persisted as JSON under platformdirs' user-config directory. Missing fields
fall back to defaults, so older configs keep working when fields are added.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from platformdirs import user_config_path, user_music_path

APP_NAME = "jack"


def _default_output_dir() -> str:
    return str(user_music_path() or (Path.home() / "Music"))


@dataclass
class Config:
    output_dir: str = field(default_factory=_default_output_dir)
    device_index: int | None = None
    device_name: str | None = None
    # None = use the device's native rate (recommended — PortAudio won't
    # resample, so forcing 44100 on a 48 kHz interface fails).
    sample_rate: int | None = None
    channels: int = 2
    # Silence-detection tunables (see audio/silence.py).
    silence_threshold_db: float = -45.0
    silence_duration_s: float = 2.5
    min_track_duration_s: float = 20.0
    # MusicBrainz identity (required by their TOS).
    musicbrainz_contact: str = "jack-vinyl-ripper@localhost"

    @classmethod
    def path(cls) -> Path:
        return user_config_path(APP_NAME, ensure_exists=True) / "config.json"

    @classmethod
    def load(cls) -> "Config":
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            data: dict[str, Any] = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))
