# jack

A terminal application for ripping vinyl records to FLAC. Built for
Arch-based Linux (developed on Garuda), driven by a `textual` TUI.

It captures audio from a USB interface in real time, auto-splits tracks by
detecting inter-track silence, looks up the album on MusicBrainz to get the
tracklist + cover art, then writes each track as a tagged 24-bit FLAC with
embedded artwork.

```
┌───────────────────────────────────────────────────────────────┐
│ Jack — Vinyl Ripper                                            │
├───────────────┬───────────────────────────────────────────────┤
│               │                                       [SIDE A] │
│  [cover art]  │  ·  1. Speak to Me                      0:07   │
│   (chafa)     │  ●  2. Breathe (In the Air)             2:43   │
│               │  ·  3. On the Run                       3:35   │
│               │  ·  4. Time                             7:01   │
│               │  ·  5. The Great Gig in the Sky         4:46   │
│               │  ·  6. Money                            6:21   │
├───────────────┴───────────────────────────────────────────────┤
│ VU  █████████████░░░░░░░  peak -12.3 dB  rms -18.7 dB           │
├───────────────────────────────────────────────────────────────┤
│ Track  ████████░░░░░░░  Overall  ██░░░░░░░░░░░  2 of 10 tracks │
├───────────────────────────────────────────────────────────────┤
│  [P] Pause  [F] Flip Record  [Q] Stop & Review                 │
└───────────────────────────────────────────────────────────────┘
```

## Requirements

- Linux (developed on Garuda / Arch-based)
- Python 3.11+
- A USB audio interface plugged into your turntable's line out
- System packages: `portaudio`, `chafa`, `flac`, `alsa-utils`, `sox`

```bash
sudo pacman -S --needed portaudio chafa flac alsa-utils sox
```

## Install

```bash
# 1. Clone and enter the repo
cd ~/Projects/jack

# 2. Create a venv (anywhere — example uses ~/.venv/jack)
python -m venv ~/.venv/jack
source ~/.venv/jack/bin/activate.fish   # or activate for bash/zsh

# 3. Install the package + Python deps
pip install -e .
```

That puts a `jack` script on your PATH (inside the venv). You can also run
`python -m jack` if you prefer.

## Usage

Plug in your interface, then:

```bash
jack
```

You'll see three screens in sequence:

### 1. Setup

- Type the artist and album, hit **Search MusicBrainz**.
- Pick a row from the results — vinyl pressings are sorted to the top.
  The tracklist + cover art load automatically.
- Confirm the **device** dropdown (PipeWire wrappers are usually the right
  pick — they accept any sample rate, raw ALSA `hw:` devices may not).
- Confirm the **output directory** and **silence threshold** (default
  `-45 dB`; tune up for noisier setups, down for very quiet vinyl).
- Click **Begin Ripping**.

### 2. Ripping

- Drop the needle. Each track shows a status icon as it progresses:
  `·` waiting → `●` recording → `⚙` encoding → `✓` done (`!` means the
  recorded length diverged from MusicBrainz by more than 15% — usually
  harmless, sometimes worth checking).
- The VU meter shows live peak and RMS in dBFS.
- After Side A finishes (track count from MusicBrainz), a modal will
  ask you to flip the record. Flip, press **Enter**, and it resumes
  with track numbering continuing into Side B.
- Keys: **P** pause/resume, **F** force-flip (for releases where
  MusicBrainz didn't tag A/B sides), **Q** stop & review.

### 3. Completion

- Summary + per-track file list with sizes.
- **O** opens the album folder via `xdg-open`.
- **R** resets state and returns to setup for the next record.
- **Q** quits.

## Output

```
~/Music/                          # configurable
└── Pink Floyd/
    └── The Dark Side of the Moon/
        ├── 01 - Speak to Me.flac
        ├── 02 - Breathe (In the Air).flac
        └── ...
```

Each file is 24-bit FLAC at the device's native sample rate (usually 48 kHz)
with Vorbis comments (`TITLE`, `ARTIST`, `ALBUM`, `ALBUMARTIST`,
`TRACKNUMBER`, `TRACKTOTAL`, `DISCNUMBER`, `DATE`) and an embedded front
cover.

## Configuration

User config lives at `~/.config/jack/config.json`. Run `jack --config` to
print the path. Fields persist across runs (last-used device, output dir,
silence threshold). You can edit it directly if you want.

| Field                  | Meaning                                      |
|------------------------|----------------------------------------------|
| `output_dir`           | Where FLACs are written                      |
| `device_index`         | Last-used input device index                 |
| `silence_threshold_db` | dBFS below which audio is treated as silence |
| `silence_duration_s`   | How long silence must last to split (2.5 s)  |
| `min_track_duration_s` | Minimum track length, prevents false splits  |
| `musicbrainz_contact`  | Used in the MB User-Agent (their TOS)        |

## Command-line flags

```
jack --version          # show version
jack --config           # print config file path
jack --log-level DEBUG  # verbose logging to ~/.cache/jack/jack.log
jack --fixture          # offline mode: use bundled MB JSON (skip API)
jack --skip-preflight   # bypass device check (for headless tests)
```

The `JACK_LOG` and `JACK_USE_FIXTURE` environment variables work too.

## Troubleshooting

**"Invalid sample rate" / PaErrorCode -9997.** PortAudio can't open the
device at the configured rate. Leave `sample_rate` unset (the default) so
jack uses the device's native rate.

**MusicBrainz returns 503.** MB has scheduled maintenance windows. Use
`jack --fixture` to drive the UI offline with bundled test data, or wait
and try again.

**Tracks split mid-song / don't split at all.** Tune the silence threshold
on the setup screen. The live audio level in the VU meter is your guide —
during inter-track silence it should sit well below your threshold. For
noisy turntables, `-40` dB may suit better; for very clean setups, `-50`.

**Logs.** `~/.cache/jack/jack.log` (rotating doesn't exist yet; truncate
it manually if it grows). `jack --log-level DEBUG` adds detector / encoder
verbosity.

## Development

The project ships a stack of per-stage smoke tests under `scripts/`:

```bash
python scripts/detect_devices.py        # confirm sounddevice sees the USB IF
python scripts/capture_test.py 16       # capture 3s, print peak/RMS
python scripts/silence_test.py synthetic # deterministic detector test
python scripts/silence_test.py live 16  # live detector against the mic
python scripts/mb_test.py --fixture     # MB parsing against bundled JSON
python scripts/artwork_test.py --mbid <release-mbid>
python scripts/encoder_test.py          # synthesize tone → FLAC → verify
python scripts/ripping_screen_test.py   # full headless rip pipeline
```

The headless `ripping_screen_test.py` exercises the whole TUI + audio
pipeline end-to-end via Textual's `app.run_test()` with a monkey-patched
capture stream. Useful for catching regressions without hardware.

## License

MIT.
