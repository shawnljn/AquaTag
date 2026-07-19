# AquaTag annotation tool

`VideoLabeler_QT.py` displays synchronized video beside accelerometer and gyroscope traces so researchers can inspect recordings frame by frame and mark swimming events efficiently. The optimized version keeps the original PySide6 interface and keyboard workflow while avoiding continuous full-plot redraws.

## Install

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The current test pass used Python 3.11.2, PySide6 6.11.1, pyqtgraph 0.14.0, NumPy 2.4.6, and pandas 3.0.3 on macOS.

## Run

```bash
python VideoLabeler_QT.py
```

## Download the desktop app

Packaged releases are published on the repository's [Releases page](https://github.com/shawnljn/AquaTag/releases). Choose the archive matching your computer:

- `macOS-arm64` for Apple-silicon Macs
- `macOS-x86_64` for Intel Macs
- `Windows-x86_64` for 64-bit Windows

Extract the archive before launching AquaTag Labeler. The initial macOS packages are ad-hoc signed but not Apple-notarized, so macOS may require selecting **Open Anyway** in **System Settings → Privacy & Security** on first launch. Each archive has a matching SHA-256 checksum file.

Application settings are stored in the operating system's per-user application-data directory. Label files remain beside the selected video.

## Build a desktop package

Create a clean Python 3.11 environment, then run:

```bash
python -m pip install -r requirements-release.txt
pyside6-deploy VideoLabeler_QT.py --name "AquaTag Labeler" --mode onefile \
  --extra-modules=OpenGL,OpenGLWidgets,Svg,PrintSupport --force
```

On macOS this produces `AquaTag Labeler.app`; on Windows it produces `AquaTag Labeler.exe`. Packages must be built on their target operating system. Pushing a tag named `annotation-tool-v*` runs the packaging workflow and publishes all supported downloads as a GitHub Release.

## Workflow

1. Select **Load Video**.
2. Select **Load IMU Folder**. Parsing runs in the background, so the window remains responsive on long recordings.
3. Enter the video's start time or select **Start = IMU Min**.
4. Use **Fine sync** to correct a remaining offset in 10 ms steps.
5. Play, scrub, or use the arrow keys to inspect the synchronized traces.
6. Mark events with the keyboard and select **Save Labels**.

Naive date/time input uses the computer's local timezone. Set `AQUATAG_TIMEZONE` to an IANA name such as `America/New_York` when a fixed timezone is required.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `S` | Stroke |
| `R` | Rest |
| `P` | Wall push |
| `T` | Turn |
| `Backspace` | Undo the latest unsaved label |
| `Space` | Play/pause |
| `Left` | Move backward 5 seconds |
| `Right` | Move forward 5 seconds |

## Supported sensor logs

- CSV files with common accelerometer/gyroscope column aliases and a numeric or ISO timestamp column
- Headerless comma- or whitespace-delimited text with 6 or 9 sensor channels followed by a timestamp
- Timestamp units expressed as seconds, milliseconds, microseconds, or nanoseconds

The loader combines files in timestamp order. Saved events are appended to `labels.csv` and `labels.log` beside the selected video; each save writes only labels created since the previous save.

## Test

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

The test suite covers timestamp parsing, both supported log layouts, fine-sync mapping, shortcut isolation, repeated saves, accelerated-playback stability, and plotting a synthetic three-hour recording.

## Long-recording optimizations

- Sensor files load on a worker thread rather than freezing the interface.
- Plot refreshes follow media position events and are coalesced to a bounded rate.
- Rapid speed-slider events are coalesced before touching the media decoder, and the selected rate is restored after loading a video.
- Paused video no longer triggers identical redraws.
- Sensor arrays are cached instead of repeatedly converted from pandas columns.
- Traces are capped relative to display width and use pyqtgraph peak downsampling.
- Headerless text uses pandas' C parser instead of its slower regex/Python parser.
- Per-video start time, fine-sync offset, and resume position are restored after media is ready.
