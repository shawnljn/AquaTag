import sys
import os
import json
import glob
import time
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QUrl, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QLineEdit, QSplitter, QSlider, QStatusBar, QListWidget,
    QListWidgetItem, QMessageBox, QStyle, QComboBox, QSpinBox
)
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

import pyqtgraph as pg
from datetime import datetime, timezone

APP_TITLE = "IMU Video Labeler (Qt + PyQtGraph)"
SESSION_FILE = "session_state.json"
LABELS_CSV = "labels.csv"
LABELS_LOG = "labels.log"

DEFAULT_TIMEZONE = os.environ.get("AQUATAG_TIMEZONE")

def ms_to_iso(ms: int) -> str:
    try:
        local_time = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone()
        return local_time.strftime("%Y-%m-%d %H:%M:%S.%f %Z")
    except Exception:
        return str(ms)

def parse_start_time_to_utc_ms(
    s: str, local_tz_name: Optional[str] = DEFAULT_TIMEZONE
) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        val = float(s)
        if val > 1e12:   # ms
            return int(val)
        else:            # seconds
            return int(val * 1000.0)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            if local_tz_name:
                from zoneinfo import ZoneInfo
                local_tz = ZoneInfo(local_tz_name)
            else:
                local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            dt = dt.replace(tzinfo=local_tz)
        utc_dt = dt.astimezone(timezone.utc)
        return int(utc_dt.timestamp() * 1000.0)
    except Exception:
        pass
    fmts = ["%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"]
    if local_tz_name:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo(local_tz_name)
    else:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=local_tz)
            utc_dt = dt.astimezone(timezone.utc)
            return int(utc_dt.timestamp() * 1000.0)
        except Exception:
            continue
    return None

@dataclass
class LabelEvent:
    video_ms: int
    imu_utc_ms: int
    label: str
    frame: Optional[int]
    created_at_ms: int

# ---------------- IMU loader (fixed for 9-column format) ----------------

_TS_NUMERIC_MS_CANDIDATES = ["timestamp_ms", "ts_ms", "time_ms", "epoch_ms"]
_TS_NUMERIC_CANDIDATES = ["timestamp", "time", "t", "epoch", "unix"]
_TS_STRING_CANDIDATES  = ["datetime", "date", "iso_time"]

_COL_ALIASES = {
    "acc_x": ["acc_x", "ax", "accx", "accel_x", "a_x"],
    "acc_y": ["acc_y", "ay", "accy", "accel_y", "a_y"],
    "acc_z": ["acc_z", "az", "accz", "accel_z", "a_z"],
    "gyr_x": ["gyr_x", "gx", "gyrox", "gyro_x", "g_x"],
    "gyr_y": ["gyr_y", "gy", "gyroy", "gyro_y", "g_y"],
    "gyr_z": ["gyr_z", "gz", "gyroz", "gyro_z", "g_z"],
    "mag_x": ["mag_x", "mx", "magx", "magnet_x", "m_x"],
    "mag_y": ["mag_y", "my", "magy", "magnet_y", "m_y"],
    "mag_z": ["mag_z", "mz", "magz", "magnet_z", "m_z"],
}

def _normalize_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df

def _first_present(df: pd.DataFrame, names: List[str]) -> Optional[str]:
    for n in names:
        if n in df.columns:
            return n
    return None

def _to_numeric_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")

def _infer_ts_col_from_headerless(df_nohdr: pd.DataFrame) -> int:
    """Infer which column is the timestamp - typically the last one or the one with largest values"""
    # Inspecting a bounded sample avoids a full-column reduction on multi-hour logs.
    sample = df_nohdr.iloc[: min(4096, len(df_nohdr))]
    medians = sample.median(numeric_only=True)
    # Look for columns with values > 1e11 (likely timestamps in ms)
    candidates = [i for i, m in enumerate(medians) if m > 1e11]
    if candidates:
        # Return the one with the largest median (most likely to be timestamp)
        return int(max(candidates, key=lambda i: medians[i]))
    # Default to last column
    return df_nohdr.shape[1] - 1

def _load_headerless_txt(path: str) -> Optional[pd.DataFrame]:
    """Load a headerless text log with 6/9 sensor channels and a timestamp."""
    try:
        # The Python regex parser is extremely slow on long recordings. Detect the
        # delimiter once and use pandas' C parser instead.
        first_data_line = ""
        with open(path, "r", encoding="utf-8", errors="replace") as source:
            for line in source:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    first_data_line = stripped
                    break
        separator = "," if "," in first_data_line else r"\s+"
        df = pd.read_csv(
            path,
            header=None,
            comment="#",
            engine="c",
            sep=separator,
            memory_map=True,
        )
    except Exception as e:
        print(f"[WARN] Failed reading txt {path}: {e}")
        return None

    if df.empty:
        return None

    # Convert malformed fields once while retaining the fast parser above.
    df = df.apply(pd.to_numeric, errors="coerce")

    # Drop rows that are completely NaN
    df = df.dropna(how="all")
    if df.empty:
        return None

    # Based on your sample data: 9 columns for IMU + 1 for timestamp = 10 total
    # Format: ACC(3), GYRO(3), MAG(3), TIMESTAMP(1)
    ncol = df.shape[1]

    # Infer which column is the timestamp
    ts_idx = _infer_ts_col_from_headerless(df)

    # Extract timestamp column
    ts = df.iloc[:, ts_idx].to_numpy(dtype=float)
    mask = np.isfinite(ts)
    df = df.iloc[mask].reset_index(drop=True)
    ts = ts[mask]

    if ts.size == 0:
        return None

    # Determine timestamp scale
    maxv = float(np.nanmax(ts))
    if not np.isfinite(maxv):
        return None

    # Convert to milliseconds based on magnitude
    if maxv > 1e17:      # nanoseconds
        ts_ms = ts / 1e6
    elif maxv > 1e13:    # microseconds
        ts_ms = ts / 1e3
    elif maxv > 1e11:    # milliseconds (like your sample: 1751477000293)
        ts_ms = ts
    else:                # seconds
        ts_ms = ts * 1000.0

    # Helper function to safely get column or return NaN array
    def col_or_nan(i):
        if 0 <= i < ncol and i != ts_idx:
            return df.iloc[:, i].to_numpy(dtype=float, copy=False)
        return np.full(df.shape[0], np.nan, dtype=float)

    # Map columns based on expected format
    # If we have 10 columns total (including timestamp), map them properly
    if ncol == 10:
        # Determine column indices (excluding timestamp column)
        cols = list(range(ncol))
        cols.remove(ts_idx)

        # Assuming standard order if timestamp is last: ACC(0-2), GYRO(3-5), MAG(6-8)
        # If timestamp is not last, we need to adjust indices
        if ts_idx == ncol - 1:  # Timestamp is last (most common)
            acc_x = col_or_nan(0); acc_y = col_or_nan(1); acc_z = col_or_nan(2)
            gyr_x = col_or_nan(3); gyr_y = col_or_nan(4); gyr_z = col_or_nan(5)
            mag_x = col_or_nan(6); mag_y = col_or_nan(7); mag_z = col_or_nan(8)
        else:
            # Handle case where timestamp might be in a different position
            acc_x = col_or_nan(cols[0] if len(cols) > 0 else -1)
            acc_y = col_or_nan(cols[1] if len(cols) > 1 else -1)
            acc_z = col_or_nan(cols[2] if len(cols) > 2 else -1)
            gyr_x = col_or_nan(cols[3] if len(cols) > 3 else -1)
            gyr_y = col_or_nan(cols[4] if len(cols) > 4 else -1)
            gyr_z = col_or_nan(cols[5] if len(cols) > 5 else -1)
            mag_x = col_or_nan(cols[6] if len(cols) > 6 else -1)
            mag_y = col_or_nan(cols[7] if len(cols) > 7 else -1)
            mag_z = col_or_nan(cols[8] if len(cols) > 8 else -1)
    elif ncol == 7:  # Maybe no magnetometer data
        if ts_idx == ncol - 1:
            acc_x = col_or_nan(0); acc_y = col_or_nan(1); acc_z = col_or_nan(2)
            gyr_x = col_or_nan(3); gyr_y = col_or_nan(4); gyr_z = col_or_nan(5)
            mag_x = mag_y = mag_z = np.full(df.shape[0], np.nan, dtype=float)
        else:
            cols = list(range(ncol))
            cols.remove(ts_idx)
            acc_x = col_or_nan(cols[0] if len(cols) > 0 else -1)
            acc_y = col_or_nan(cols[1] if len(cols) > 1 else -1)
            acc_z = col_or_nan(cols[2] if len(cols) > 2 else -1)
            gyr_x = col_or_nan(cols[3] if len(cols) > 3 else -1)
            gyr_y = col_or_nan(cols[4] if len(cols) > 4 else -1)
            gyr_z = col_or_nan(cols[5] if len(cols) > 5 else -1)
            mag_x = mag_y = mag_z = np.full(df.shape[0], np.nan, dtype=float)
    else:
        # Fallback: use what columns we have
        acc_x = col_or_nan(0); acc_y = col_or_nan(1); acc_z = col_or_nan(2)
        gyr_x = col_or_nan(3); gyr_y = col_or_nan(4); gyr_z = col_or_nan(5)
        mag_x = col_or_nan(6); mag_y = col_or_nan(7); mag_z = col_or_nan(8)

    # Create output dataframe with all sensor data
    out = pd.DataFrame({
        "acc_x": acc_x, "acc_y": acc_y, "acc_z": acc_z,
        "gyr_x": gyr_x, "gyr_y": gyr_y, "gyr_z": gyr_z,
        "mag_x": mag_x, "mag_y": mag_y, "mag_z": mag_z,
        "timestamp_ms": ts_ms.astype(np.int64, copy=False),
    })
    sensor_columns = [c for c in out.columns if c != "timestamp_ms"]
    out[sensor_columns] = out[sensor_columns].astype(np.float32)

    print(f"[INFO] Loaded {path}: {len(out)} rows, {ncol} columns (ts_idx={ts_idx})")
    return out

def _timestamp_ms_series(df: pd.DataFrame) -> Tuple[Optional[pd.Series], Optional[str]]:
    """Return one aligned millisecond series and its source column.

    The old loader converted each timestamp column twice. It also treated pandas'
    integer representation of ``NaT`` as a valid finite value. Keeping the series
    aligned with the frame makes row filtering both faster and correct.
    """
    name = _first_present(df, _TS_NUMERIC_MS_CANDIDATES)
    if name is not None:
        return _to_numeric_series(df[name]).astype(float), name

    name = _first_present(df, _TS_NUMERIC_CANDIDATES)
    if name is not None:
        values = _to_numeric_series(df[name]).astype(float)
        finite = values.to_numpy(dtype=float)
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            return None, name
        maxv = float(np.nanmax(finite))
        if maxv > 1e17:
            values = values / 1e6
        elif maxv > 1e13:
            values = values / 1e3
        elif maxv <= 1e11:
            values = values * 1000.0
        return values, name

    name = _first_present(df, _TS_STRING_CANDIDATES)
    if name is not None:
        parsed = pd.to_datetime(df[name], errors="coerce", utc=True)
        valid = parsed.notna()
        values = pd.Series(np.nan, index=df.index, dtype=float)
        if valid.any():
            values.loc[valid] = parsed.loc[valid].astype("int64").to_numpy() / 1e6
        return values, name

    return None, None

def _pick_column(df: pd.DataFrame, aliases: List[str]) -> Optional[pd.Series]:
    for a in aliases:
        if a in df.columns:
            return _to_numeric_series(df[a])
    return None

def load_imu_folder(folder: str) -> pd.DataFrame:
    csvs = sorted(glob.glob(os.path.join(folder, "*.csv")))
    txts = sorted(glob.glob(os.path.join(folder, "*.txt")))  # Changed from "imu*.txt" to "*.txt"
    files = csvs + txts
    if not files:
        raise FileNotFoundError("No CSV or *.txt files found in IMU folder.")
    dfs = []
    dropped_report = []
    total_rows = 0
    total_kept = 0
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".txt":
            out = _load_headerless_txt(path)
            if out is None or out.empty:
                print(f"[WARN] {os.path.basename(path)}: could not parse headerless IMU txt.")
                continue
            total_rows += len(out)
            total_kept += len(out)
            dfs.append(out)
            continue
        try:
            raw = pd.read_csv(path, low_memory=False, memory_map=True)
        except Exception as e:
            print(f"[WARN] Failed reading {path}: {e}")
            continue
        df = _normalize_cols(raw)
        total_rows += len(df)
        ts_full, ts_col = _timestamp_ms_series(df)
        if ts_full is None or ts_col is None:
            print(f"[WARN] {os.path.basename(path)}: could not determine timestamp column; skipping.")
            continue
        mask = np.isfinite(ts_full.to_numpy(dtype=float))
        kept_idx = np.nonzero(mask)[0]
        if kept_idx.size == 0:
            print(f"[WARN] {os.path.basename(path)}: all timestamps invalid; skipping.")
            continue
        dropped = len(df) - kept_idx.size
        if dropped > 0:
            dropped_report.append((os.path.basename(path), dropped))
        sel = df.iloc[kept_idx].reset_index(drop=True)
        out = pd.DataFrame()
        for canonical, aliases in _COL_ALIASES.items():
            s = _pick_column(sel, aliases)
            out[canonical] = (
                s.astype(np.float32)
                if s is not None
                else pd.Series(np.nan, index=sel.index, dtype=np.float32)
            )
        out["timestamp_ms"] = ts_full.iloc[kept_idx].to_numpy(dtype=np.int64, copy=False)
        total_kept += len(out)
        dfs.append(out)
    if not dfs:
        raise RuntimeError("No valid IMU files parsed (all had invalid or missing timestamps).")
    full = pd.concat(dfs, ignore_index=True)
    if not full["timestamp_ms"].is_monotonic_increasing:
        full.sort_values("timestamp_ms", inplace=True, kind="mergesort")
    full.reset_index(drop=True, inplace=True)
    if dropped_report:
        msg = "; ".join([f"{name}: dropped {n}" for name, n in dropped_report])
        print(f"[INFO] Dropped rows with invalid timestamps → {msg}")
    print(f"[INFO] IMU rows loaded: kept {total_kept} / read {total_rows} from {len(files)} file(s)")
    return full


class ImuLoadWorker(QtCore.QObject):
    """Parse large sensor folders without blocking video playback or the UI."""

    finished = Signal(object, str, int)
    failed = Signal(str)

    def __init__(self, folder: str):
        super().__init__()
        self.folder = folder

    @Slot()
    def run(self):
        try:
            frame = load_imu_folder(self.folder)
            file_count = len(glob.glob(os.path.join(self.folder, "*.csv"))) + len(
                glob.glob(os.path.join(self.folder, "*.txt"))
            )
            self.finished.emit(frame, self.folder, file_count)
        except Exception as exc:
            self.failed.emit(str(exc))


# ---------------- GUI + Player ----------------

class VideoGraphicsView(QVideoWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setMinimumSize(640, 360)

class MainWindow(QMainWindow):
    label_made = Signal(LabelEvent)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1400, 900)

        self.video_path: Optional[str] = None
        self.imu_df: pd.DataFrame = pd.DataFrame()
        self.imu_ts_ms: Optional[np.ndarray] = None
        self.imu_arrays: Dict[str, np.ndarray] = {}
        self.imu_min_ms: Optional[int] = None
        self.imu_max_ms: Optional[int] = None
        self.video_start_utc_ms: Optional[int] = None
        self.sync_offset_ms = 0
        self.labels: List[LabelEvent] = []
        self._saved_label_count = 0
        self._pending_video_position: Optional[int] = None
        self._resume_after_scrub = False
        self._requested_playback_rate = 1.0
        self._applied_playback_rate = 1.0
        self._speed_apply_delay_ms = 120
        self._imu_thread: Optional[QtCore.QThread] = None
        self._imu_worker: Optional[ImuLoadWorker] = None
        self.session_state = self._read_session_state()

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(None)
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.errorOccurred.connect(self.on_media_error)
        self.player.playbackRateChanged.connect(self.on_playback_rate_changed)

        self.video_widget = VideoGraphicsView(self)
        self.player.setVideoOutput(self.video_widget)

        self.progress = QSlider(Qt.Horizontal)
        self.progress.setRange(0, 0)
        self.progress.sliderPressed.connect(self.on_slider_pressed)
        self.progress.sliderReleased.connect(self.on_slider_released)
        self.progress.valueChanged.connect(self.on_slider_value_changed)
        self.slider_user_is_dragging = False

        self.btn_load = QPushButton("Load Video")
        self.btn_load_imu = QPushButton("Load IMU Folder")
        self.btn_save = QPushButton("Save Labels")
        self.btn_open_dir = QPushButton("Open Label Dir")
        self.btn_play = QPushButton(self.style().standardIcon(QStyle.SP_MediaPlay), "Play")
        self.btn_pause = QPushButton(self.style().standardIcon(QStyle.SP_MediaPause), "Pause")

        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setMinimum(25)
        self.speed_slider.setMaximum(300)
        self.speed_slider.setSingleStep(25)
        self.speed_slider.setPageStep(25)
        self.speed_slider.setTickInterval(25)
        self.speed_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.speed_slider.setValue(100)
        self.speed_slider.setToolTip(
            "Playback speed (0.25x–3.00x). Changes are applied after dragging stops."
        )
        self.speed_label = QLabel("Speed: 1.00x")

        self.start_time_edit = QLineEdit()
        self.start_time_edit.setPlaceholderText("Video start time (unix s/ms, ISO, or local time)")
        self.btn_apply_start = QPushButton("Apply Start Time")
        self.btn_start_from_imu_min = QPushButton("Start = IMU Min")
        self.sync_offset_spin = QSpinBox()
        self.sync_offset_spin.setRange(-3_600_000, 3_600_000)
        self.sync_offset_spin.setSingleStep(10)
        self.sync_offset_spin.setSuffix(" ms")
        self.sync_offset_spin.setToolTip(
            "Fine adjustment added to the video-to-sensor time mapping."
        )

        self.lbl_imu_range = QLabel("IMU range: —")
        self.lbl_window_info = QLabel("Window: —")

        # Add time window selector
        self.window_size_label = QLabel("Plot Window:")
        self.window_size_combo = QComboBox()
        self.window_size_combo.addItems(["10s", "20s", "30s", "60s"])
        self.window_size_combo.setCurrentText("10s")
        self.window_size_combo.currentTextChanged.connect(self.on_window_size_changed)
        self.plot_window_ms = 10000  # Default 10 seconds in milliseconds

        self.recent_labels = QListWidget()

        # Create keyboard shortcuts help widget
        shortcuts_text = """
        <b>Keyboard Shortcuts:</b><br>
        <br>
        <b>Labeling:</b><br>
        S - Stroke<br>
        R - Rest<br>
        P - Wallpush<br>
        T - Turn<br>
        Backspace - Undo last label<br>
        <br>
        <b>Playback:</b><br>
        Space - Play/Pause<br>
        ← - Skip back 5 sec<br>
        → - Skip forward 5 sec<br>
        """
        self.shortcuts_label = QLabel(shortcuts_text)
        self.shortcuts_label.setStyleSheet("""
            QLabel {
                background-color: #2b2b2b;
                color: #ffffff;
                padding: 10px;
                border: 1px solid #444;
                border-radius: 5px;
                font-size: 13px;
            }
        """)

        # Antialiasing six continuously updated traces is expensive and provides
        # little benefit at video-playback scale.
        pg.setConfigOptions(antialias=False)
        self.acc_plot = pg.PlotWidget(title="Accelerometer")
        self.gyr_plot = pg.PlotWidget(title="Gyroscope")
        for pltw in (self.acc_plot, self.gyr_plot):
            pltw.showGrid(x=True, y=True, alpha=0.3)

        self.acc_curves = {
            "x": self.acc_plot.plot(pen=pg.mkPen('r', width=2), name="acc_x"),
            "y": self.acc_plot.plot(pen=pg.mkPen('g', width=2), name="acc_y"),
            "z": self.acc_plot.plot(pen=pg.mkPen('b', width=2), name="acc_z"),
        }
        self.gyr_curves = {
            "x": self.gyr_plot.plot(pen=pg.mkPen('r', width=2), name="gyr_x"),
            "y": self.gyr_plot.plot(pen=pg.mkPen('g', width=2), name="gyr_y"),
            "z": self.gyr_plot.plot(pen=pg.mkPen('b', width=2), name="gyr_z"),
        }
        for curve in list(self.acc_curves.values()) + list(self.gyr_curves.values()):
            curve.setClipToView(True)
            curve.setDownsampling(auto=True, method="peak")

        self.acc_now = None
        self.gyr_now = None
        self._plots_ready = False
        QTimer.singleShot(0, self._build_plot_overlays)

        # === Performance / stability guards ===
        self.max_plot_hz = 15.0
        self._plot_interval_ms = max(1, round(1000 / self.max_plot_hz))
        self._pending_plot_ms: Optional[int] = None
        self._last_rendered_video_ms: Optional[int] = None
        self._plotting = False  # reentrancy guard

        # Session write throttle
        self._last_session_write = 0.0
        self._session_write_interval = 3.0  # seconds

        self._build_layout()

        self.status = QStatusBar()
        self.setStatusBar(self.status)

        self._install_shortcuts()

        self.plot_timer = QTimer(self)
        self.plot_timer.setSingleShot(True)
        self.plot_timer.setInterval(self._plot_interval_ms)
        self.plot_timer.timeout.connect(self._flush_plot_update)

        # Reconfiguring a multimedia decoder for every intermediate slider value
        # causes stalls on long/GOP-compressed videos. Coalesce drag events and
        # apply only the final requested rate.
        self.speed_apply_timer = QTimer(self)
        self.speed_apply_timer.setSingleShot(True)
        self.speed_apply_timer.setInterval(self._speed_apply_delay_ms)
        self.speed_apply_timer.timeout.connect(self._apply_requested_playback_rate)

        self.restore_last_session()

    def _build_layout(self):
        # Left column with vertical splitter between video and plots
        video_section = QWidget()
        video_layout = QVBoxLayout(video_section)
        video_layout.addWidget(self.video_widget)
        video_layout.addWidget(self.progress)

        # Plots section
        plots_section = QWidget()
        plots_layout = QVBoxLayout(plots_section)
        plots_layout.addWidget(self.acc_plot)
        plots_layout.addWidget(self.gyr_plot)

        # Vertical splitter for video vs plots with custom styling
        left_splitter = QSplitter(Qt.Vertical)
        left_splitter.addWidget(video_section)
        left_splitter.addWidget(plots_section)
        left_splitter.setStretchFactor(0, 3)  # Video gets 3/5 initially
        left_splitter.setStretchFactor(1, 2)  # Plots get 2/5 initially

        # Style the splitter handle to make it more visible and easier to grab
        left_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #555;
                border: 1px solid #777;
                height: 8px;
            }
            QSplitter::handle:hover {
                background-color: #777;
                border: 1px solid #999;
            }
            QSplitter::handle:pressed {
                background-color: #999;
            }
        """)
        left_splitter.setHandleWidth(8)  # Make the handle wider for easier grabbing

        # Add IMU range, window info, and window size selector below plots
        info_widget = QWidget()
        info_layout = QHBoxLayout(info_widget)
        info_layout.addWidget(self.lbl_imu_range)
        info_layout.addWidget(self.lbl_window_info)
        info_layout.addSpacing(20)
        info_layout.addWidget(self.window_size_label)
        info_layout.addWidget(self.window_size_combo)
        info_layout.addStretch()

        # Complete left column
        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.addWidget(left_splitter, 1)
        left_layout.addWidget(info_widget)

        # Right column: Keyboard shortcuts and labels history
        right_column = QWidget()
        right_layout = QVBoxLayout(right_column)
        right_layout.addWidget(self.shortcuts_label)
        right_layout.addWidget(QLabel("<b>Label History:</b>"))
        right_layout.addWidget(self.recent_labels, 1)

        # Main horizontal splitter
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(left_column)
        main_splitter.addWidget(right_column)
        main_splitter.setStretchFactor(0, 3)  # Left column takes 3/4 of width
        main_splitter.setStretchFactor(1, 1)  # Right column takes 1/4 of width

        # Top controls in two rows
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setSpacing(5)  # Reduce spacing between rows

        # First row: File operations and start time
        row1 = QWidget()
        row1_layout = QHBoxLayout(row1)
        row1_layout.setContentsMargins(0, 0, 0, 0)
        row1_layout.addWidget(self.btn_load)
        row1_layout.addWidget(self.btn_load_imu)
        row1_layout.addWidget(QLabel("Start time:"))
        row1_layout.addWidget(self.start_time_edit, 1)
        row1_layout.addWidget(self.btn_apply_start)
        row1_layout.addWidget(self.btn_start_from_imu_min)
        row1_layout.addWidget(QLabel("Fine sync:"))
        row1_layout.addWidget(self.sync_offset_spin)

        # Second row: Playback controls and file management
        row2 = QWidget()
        row2_layout = QHBoxLayout(row2)
        row2_layout.setContentsMargins(0, 0, 0, 0)
        row2_layout.addWidget(self.btn_play)
        row2_layout.addWidget(self.btn_pause)
        row2_layout.addWidget(QLabel("Speed:"))
        row2_layout.addWidget(self.speed_slider)
        row2_layout.addWidget(self.speed_label)
        row2_layout.addSpacing(20)
        row2_layout.addWidget(self.btn_save)
        row2_layout.addWidget(self.btn_open_dir)
        row2_layout.addStretch()

        top_layout.addWidget(row1)
        top_layout.addWidget(row2)

        wrapper = QWidget()
        wrap_v = QVBoxLayout(wrapper)
        wrap_v.addWidget(top)
        wrap_v.addWidget(main_splitter, 1)

        self.setCentralWidget(wrapper)

        # Connect all button signals to their slots
        self.btn_load.clicked.connect(self.on_load_video)
        self.btn_load_imu.clicked.connect(self.on_load_imu_folder)
        self.btn_apply_start.clicked.connect(self.on_apply_start_time)
        self.btn_start_from_imu_min.clicked.connect(self.on_start_from_imu_min)
        self.btn_play.clicked.connect(self.on_play_clicked)
        self.btn_pause.clicked.connect(self.on_pause_clicked)
        self.btn_save.clicked.connect(self.on_save_labels)
        self.btn_open_dir.clicked.connect(self.on_open_label_dir)
        self.speed_slider.valueChanged.connect(self.on_speed_changed)
        self.speed_slider.sliderReleased.connect(self._apply_requested_playback_rate)
        self.sync_offset_spin.valueChanged.connect(self.on_sync_offset_changed)

    def _build_plot_overlays(self):
        if self._plots_ready:
            return
        self.acc_now = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('y', width=2))
        self.gyr_now = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('y', width=2))
        self.acc_plot.addItem(self.acc_now, ignoreBounds=True)
        self.gyr_plot.addItem(self.gyr_now, ignoreBounds=True)
        self._plots_ready = True

    def _install_shortcuts(self):
        sc_stroke = QShortcut(QKeySequence("S"), self)
        sc_rest = QShortcut(QKeySequence("R"), self)
        sc_wallpush = QShortcut(QKeySequence("P"), self)
        sc_turn = QShortcut(QKeySequence("T"), self)
        sc_undo = QShortcut(QKeySequence(Qt.Key_Backspace), self)
        sc_stroke.activated.connect(lambda: self._run_shortcut(lambda: self.make_label("Stroke")))
        sc_rest.activated.connect(lambda: self._run_shortcut(lambda: self.make_label("Rest")))
        sc_wallpush.activated.connect(lambda: self._run_shortcut(lambda: self.make_label("Wallpush")))
        sc_turn.activated.connect(lambda: self._run_shortcut(lambda: self.make_label("Turn")))
        sc_undo.activated.connect(lambda: self._run_shortcut(self.undo_label))
        sc_space = QShortcut(QKeySequence(Qt.Key_Space), self)
        sc_left = QShortcut(QKeySequence(Qt.Key_Left), self)
        sc_right = QShortcut(QKeySequence(Qt.Key_Right), self)
        sc_space.activated.connect(lambda: self._run_shortcut(self.toggle_play_pause))
        sc_left.activated.connect(lambda: self._run_shortcut(lambda: self.nudge(-5000)))
        sc_right.activated.connect(lambda: self._run_shortcut(lambda: self.nudge(+5000)))
        self.shortcuts = [
            sc_stroke,
            sc_rest,
            sc_wallpush,
            sc_turn,
            sc_undo,
            sc_space,
            sc_left,
            sc_right,
        ]

    def _run_shortcut(self, action):
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QSpinBox, QComboBox)):
            return
        action()

    def _read_session_state(self):
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _write_session_state(self, force=False):
        now = time.time()
        if not force and (now - self._last_session_write) < self._session_write_interval:
            return
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(self.session_state, f, indent=2)
            self._last_session_write = now
        except Exception as e:
            print("[WARN] failed saving session:", e)

    def restore_last_session(self):
        last_start = self.session_state.get("last_start_time_input", "")
        if last_start:
            self.start_time_edit.setText(last_start)
        try:
            saved_rate = float(self.session_state.get("playback_rate", 1.0))
        except (TypeError, ValueError):
            saved_rate = 1.0
        if not math.isfinite(saved_rate):
            saved_rate = 1.0
        saved_rate = min(3.0, max(0.25, saved_rate))
        slider_value = int(round(saved_rate * 100))
        self.speed_slider.blockSignals(True)
        self.speed_slider.setValue(slider_value)
        self.speed_slider.blockSignals(False)
        self._requested_playback_rate = self.speed_slider.value() / 100.0
        self.speed_label.setText(f"Speed: {self._requested_playback_rate:.2f}x")
        self._apply_requested_playback_rate()

    @staticmethod
    def _video_state_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _video_state_for(self, path: str) -> dict:
        videos = self.session_state.setdefault("videos", {})
        key = self._video_state_key(path)
        if key not in videos:
            # Migrate state written by the original basename-only implementation.
            videos[key] = dict(videos.get(os.path.basename(path), {}))
        return videos[key]

    def _has_unsaved_labels(self) -> bool:
        return len(self.labels) > self._saved_label_count

    def _confirm_video_change(self) -> bool:
        if not self._has_unsaved_labels():
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved labels",
            "This video has unsaved labels. Continue without saving them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    @Slot()
    def on_load_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video",
            "",
            "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)",
        )
        if not path:
            return
        if self.video_path and not self._confirm_video_change():
            return

        self.player.pause()
        self.video_path = path
        self.labels.clear()
        self._saved_label_count = 0
        self.recent_labels.clear()
        self._last_rendered_video_ms = None

        video_state = self._video_state_for(path)
        saved_start = video_state.get("start_utc_ms")
        if saved_start is not None:
            self.video_start_utc_ms = int(saved_start)
            self.start_time_edit.setText(str(self.video_start_utc_ms))
        else:
            self.video_start_utc_ms = None

        self.sync_offset_ms = int(video_state.get("sync_offset_ms", 0))
        self.sync_offset_spin.blockSignals(True)
        self.sync_offset_spin.setValue(self.sync_offset_ms)
        self.sync_offset_spin.blockSignals(False)
        self._pending_video_position = int(video_state.get("position_ms", 0))

        url = QUrl.fromLocalFile(path)
        self.player.setSource(url)
        self.statusBar().showMessage(f"Loaded video: {path}")

    @Slot()
    def on_load_imu_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select IMU Folder")
        if not folder:
            return
        if self._imu_thread is not None and self._imu_thread.isRunning():
            QMessageBox.information(self, "IMU Load", "A sensor folder is already loading.")
            return

        self.btn_load_imu.setEnabled(False)
        self.statusBar().showMessage(f"Loading sensor files from {folder} …")
        self._imu_thread = QtCore.QThread(self)
        self._imu_worker = ImuLoadWorker(folder)
        self._imu_worker.moveToThread(self._imu_thread)
        self._imu_thread.started.connect(self._imu_worker.run)
        self._imu_worker.finished.connect(self._on_imu_loaded)
        self._imu_worker.failed.connect(self._on_imu_load_failed)
        self._imu_worker.finished.connect(self._imu_thread.quit)
        self._imu_worker.failed.connect(self._imu_thread.quit)
        self._imu_worker.finished.connect(self._imu_worker.deleteLater)
        self._imu_worker.failed.connect(self._imu_worker.deleteLater)
        self._imu_thread.finished.connect(self._on_imu_thread_finished)
        self._imu_thread.finished.connect(self._imu_thread.deleteLater)
        self._imu_thread.start()

    @Slot(object, str, int)
    def _on_imu_loaded(self, df: pd.DataFrame, folder: str, n_files: int):
        self.imu_df = df
        self.imu_ts_ms = np.ascontiguousarray(
            self.imu_df["timestamp_ms"].to_numpy(dtype=np.int64, copy=False)
        )
        self.imu_arrays = {
            name: self.imu_df[name].to_numpy(copy=False)
            for name in ("acc_x", "acc_y", "acc_z", "gyr_x", "gyr_y", "gyr_z")
            if name in self.imu_df.columns
        }
        self.imu_min_ms = int(self.imu_ts_ms.min())
        self.imu_max_ms = int(self.imu_ts_ms.max())
        self.lbl_imu_range.setText(
            f"IMU range: {ms_to_iso(self.imu_min_ms)}  →  {ms_to_iso(self.imu_max_ms)}"
        )
        self.statusBar().showMessage(f"Loaded IMU: {len(df)} rows from {n_files} file(s) in {folder}")
        self.request_plot_update(self.player.position(), immediate=True)

    @Slot(str)
    def _on_imu_load_failed(self, message: str):
        QMessageBox.critical(self, "IMU Load Error", message)
        self.statusBar().showMessage("Sensor-folder load failed")

    @Slot()
    def _on_imu_thread_finished(self):
        self.btn_load_imu.setEnabled(True)
        self._imu_worker = None
        self._imu_thread = None

    @Slot()
    def on_apply_start_time(self):
        s = self.start_time_edit.text().strip()
        utc_ms = parse_start_time_to_utc_ms(s)
        if utc_ms is None:
            QMessageBox.warning(
                self,
                "Invalid Time",
                "Could not parse the start time. Try unix s/ms or ISO format.",
            )
            return
        self.video_start_utc_ms = utc_ms
        self.statusBar().showMessage(f"Video start set (UTC ms): {utc_ms} ({ms_to_iso(utc_ms)})")
        self.session_state["last_start_time_input"] = s
        if self.video_path:
            self._video_state_for(self.video_path)["start_utc_ms"] = utc_ms
        self._write_session_state(force=True)
        self.request_plot_update(self.player.position(), immediate=True)

    @Slot()
    def on_start_from_imu_min(self):
        if self.imu_min_ms is None:
            QMessageBox.information(self, "No IMU", "Load IMU first.")
            return
        self.video_start_utc_ms = int(self.imu_min_ms)
        # Update the text field to show the timestamp
        self.start_time_edit.setText(str(self.video_start_utc_ms))
        self.statusBar().showMessage(
            "Video start set to IMU min: "
            f"{self.video_start_utc_ms} ({ms_to_iso(self.video_start_utc_ms)})"
        )
        # Persist
        self.session_state["last_start_time_input"] = str(self.video_start_utc_ms)
        if self.video_path:
            self._video_state_for(self.video_path)["start_utc_ms"] = int(self.video_start_utc_ms)
        self._write_session_state(force=True)
        self.request_plot_update(self.player.position(), immediate=True)

    @Slot()
    def on_play_clicked(self):
        self.player.play()
        self.statusBar().showMessage("Playing")

    @Slot()
    def on_pause_clicked(self):
        self.player.pause()
        self.statusBar().showMessage("Paused")

    def toggle_play_pause(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def nudge(self, delta_ms: int):
        upper = self.player.duration() if self.player.duration() > 0 else 2_147_483_647
        pos = min(upper, max(0, self.player.position() + int(delta_ms)))
        self.player.setPosition(pos)

    @Slot(int)
    def on_position_changed(self, pos_ms: int):
        if not self.slider_user_is_dragging:
            self.progress.blockSignals(True)
            self.progress.setValue(pos_ms)
            self.progress.blockSignals(False)
        self.request_plot_update(pos_ms)
        if self.video_path:
            self._video_state_for(self.video_path)["position_ms"] = int(pos_ms)
            self._write_session_state()

    @Slot(int)
    def on_duration_changed(self, dur_ms: int):
        self.progress.setRange(0, int(max(0, dur_ms)))
        self._apply_pending_seek()

    def on_media_status_changed(self, status):
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._apply_pending_seek()
            # Some backends reset the playback rate while replacing the source.
            self._apply_requested_playback_rate()

    def _apply_pending_seek(self):
        if self._pending_video_position is None or self.player.duration() <= 0:
            return
        position = min(max(0, self._pending_video_position), self.player.duration())
        self._pending_video_position = None
        self.player.setPosition(position)
        self.request_plot_update(position, immediate=True)

    @Slot()
    def on_media_error(self):
        self.statusBar().showMessage(f"Media error: {self.player.errorString()}")

    def on_slider_pressed(self):
        self.slider_user_is_dragging = True
        self._resume_after_scrub = (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        if self._resume_after_scrub:
            self.player.pause()

    def on_slider_released(self):
        self.slider_user_is_dragging = False
        self.player.setPosition(self.progress.value())
        self.request_plot_update(self.progress.value(), immediate=True)
        if self._resume_after_scrub:
            self._resume_after_scrub = False
            self.player.play()

    def on_slider_value_changed(self, val: int):
        if self.slider_user_is_dragging:
            self.request_plot_update(val)

    def on_speed_changed(self, value: int):
        self._requested_playback_rate = min(3.0, max(0.25, value / 100.0))
        self.speed_label.setText(f"Speed: {self._requested_playback_rate:.2f}x")
        self.speed_apply_timer.start()

    @Slot()
    def _apply_requested_playback_rate(self):
        self.speed_apply_timer.stop()
        rate = self._requested_playback_rate
        if not math.isclose(self.player.playbackRate(), rate, abs_tol=1e-6):
            self.player.setPlaybackRate(rate)
        self.session_state["playback_rate"] = rate
        self._write_session_state()

    @Slot(float)
    def on_playback_rate_changed(self, rate: float):
        self._applied_playback_rate = float(rate)
        self.speed_label.setText(f"Speed: {rate:.2f}x")

    def on_window_size_changed(self, text: str):
        """Handle changes to the plot window size"""
        size_map = {"10s": 10000, "20s": 20000, "30s": 30000, "60s": 60000}
        self.plot_window_ms = size_map.get(text, 10000)
        self.request_plot_update(self.player.position(), immediate=True)

    @Slot(int)
    def on_sync_offset_changed(self, value: int):
        self.sync_offset_ms = int(value)
        if self.video_path:
            self._video_state_for(self.video_path)["sync_offset_ms"] = self.sync_offset_ms
        self._write_session_state()
        self.request_plot_update(self.player.position(), immediate=True)

    def make_label(self, label_name: str):
        if self.video_start_utc_ms is None:
            QMessageBox.information(self, "Need start time", "Please set the video start time first.")
            return
        video_ms = int(self.player.position())
        imu_ms = int(self.video_start_utc_ms + video_ms + self.sync_offset_ms)
        frame = None
        evt = LabelEvent(
            video_ms=video_ms,
            imu_utc_ms=imu_ms,
            label=label_name,
            frame=frame,
            created_at_ms=int(time.time() * 1000),
        )
        self.labels.append(evt)
        self.append_label_to_list(evt)
        self.statusBar().showMessage(f"Labeled {label_name} at {video_ms} ms (IMU UTC {imu_ms})")

    def undo_label(self):
        if len(self.labels) <= self._saved_label_count:
            self.statusBar().showMessage("There are no unsaved labels to undo")
            return
        last = self.labels.pop()
        if self.recent_labels.count() > 0:
            self.recent_labels.takeItem(self.recent_labels.count() - 1)
        self.statusBar().showMessage(f"Undid label {last.label} at {last.video_ms} ms")

    def append_label_to_list(self, evt: LabelEvent):
        item = QListWidgetItem(f"{evt.label}  |  video {evt.video_ms} ms  |  imu UTC {evt.imu_utc_ms} ms")
        self.recent_labels.addItem(item)
        self.recent_labels.scrollToBottom()

    @Slot()
    def on_save_labels(self):
        pending = self.labels[self._saved_label_count:]
        if not pending:
            QMessageBox.information(self, "No new labels", "There are no new labels to save.")
            return
        base_dir = os.path.dirname(self.video_path) if self.video_path else os.getcwd()
        csv_path = os.path.join(base_dir, LABELS_CSV)
        log_path = os.path.join(base_dir, LABELS_LOG)
        header_needed = not os.path.exists(csv_path)
        try:
            with open(csv_path, "a", encoding="utf-8") as f:
                if header_needed:
                    f.write("video_ms,imu_utc_ms,label,frame,created_at_ms\n")
                for e in pending:
                    frame_value = "" if e.frame is None else e.frame
                    f.write(
                        f"{e.video_ms},{e.imu_utc_ms},{e.label},"
                        f"{frame_value},{e.created_at_ms}\n"
                    )
            with open(log_path, "a", encoding="utf-8") as f:
                for e in pending:
                    created = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(e.created_at_ms / 1000)
                    )
                    f.write(f"[{created}] {e.label} video={e.video_ms}ms imu={e.imu_utc_ms}ms\n")
        except OSError as exc:
            QMessageBox.critical(self, "Label Save Error", str(exc))
            return
        self._saved_label_count = len(self.labels)
        self.statusBar().showMessage(f"Saved {len(pending)} new labels → {csv_path}")

    @Slot()
    def on_open_label_dir(self):
        base_dir = os.path.dirname(self.video_path) if self.video_path else os.getcwd()
        QtGui.QDesktopServices.openUrl(QUrl.fromLocalFile(base_dir))

    def update_plots_for_current_position(self):
        self.request_plot_update(self.player.position(), immediate=True)

    def request_plot_update(self, video_ms: int, immediate: bool = False):
        """Coalesce rapid playhead/scrubber events into one bounded-rate redraw."""
        self._pending_plot_ms = int(video_ms)
        if immediate:
            self._last_rendered_video_ms = None
            self.plot_timer.stop()
            self._flush_plot_update()
        elif not self.plot_timer.isActive():
            self.plot_timer.start()

    @Slot()
    def _flush_plot_update(self):
        if self._pending_plot_ms is None:
            return
        position = self._pending_plot_ms
        self._pending_plot_ms = None
        self.update_plots_for_position(position)

    def update_plots_for_position(self, video_ms: int):
        if not self._plots_ready:
            return
        if self._plotting:
            return  # guard against reentrancy
        if self.video_start_utc_ms is None or self.imu_ts_ms is None or self.imu_df.empty:
            return
        if self._last_rendered_video_ms == video_ms:
            return
        self._last_rendered_video_ms = video_ms
        self._plotting = True
        try:
            cur_imu_ms = int(self.video_start_utc_ms + video_ms + self.sync_offset_ms)
            # Use the adjustable window size
            half_window = self.plot_window_ms // 2
            start_ms = cur_imu_ms - half_window
            end_ms = cur_imu_ms + half_window
            lo = int(np.searchsorted(self.imu_ts_ms, start_ms, side="left"))
            hi = int(np.searchsorted(self.imu_ts_ms, end_ms, side="right"))
            nwin = max(0, hi - lo)
            window_sec = self.plot_window_ms / 1000
            self.lbl_window_info.setText(f"Window: {nwin} pts @ {ms_to_iso(cur_imu_ms)} ({window_sec:.0f}s)")
            if nwin <= 1:
                for c in list(self.acc_curves.values()) + list(self.gyr_curves.values()):
                    c.clear()
                if self.acc_now: self.acc_now.setPos(0.0)
                if self.gyr_now: self.gyr_now.setPos(0.0)
                return
            sl = slice(lo, hi)
            t_rel = (self.imu_ts_ms[sl].astype(np.float64) - cur_imu_ms) / 1000.0
            accx = self.imu_arrays.get("acc_x")
            accy = self.imu_arrays.get("acc_y")
            accz = self.imu_arrays.get("acc_z")
            gyrx = self.imu_arrays.get("gyr_x")
            gyry = self.imu_arrays.get("gyr_y")
            gyrz = self.imu_arrays.get("gyr_z")
            if accx is not None: accx = accx[sl]
            if accy is not None: accy = accy[sl]
            if accz is not None: accz = accz[sl]
            if gyrx is not None: gyrx = gyrx[sl]
            if gyry is not None: gyry = gyry[sl]
            if gyrz is not None: gyrz = gyrz[sl]

            # Drawing more than roughly two samples per horizontal pixel only
            # consumes CPU; pyqtgraph's peak downsampling preserves visibility.
            plot_width = max(self.acc_plot.width(), self.gyr_plot.width(), 400)
            max_pts = min(3000, max(800, plot_width * 2))
            n = t_rel.shape[0]
            if n > max_pts:
                step = math.ceil(n / max_pts)
                t_rel = t_rel[::step]
                if accx is not None: accx = accx[::step]
                if accy is not None: accy = accy[::step]
                if accz is not None: accz = accz[::step]
                if gyrx is not None: gyrx = gyrx[::step]
                if gyry is not None: gyry = gyry[::step]
                if gyrz is not None: gyrz = gyrz[::step]
            def set_curve(curve, y):
                if y is None:
                    curve.setData([], [])
                else:
                    curve.setData(t_rel, y)
            set_curve(self.acc_curves["x"], accx)
            set_curve(self.acc_curves["y"], accy)
            set_curve(self.acc_curves["z"], accz)
            set_curve(self.gyr_curves["x"], gyrx)
            set_curve(self.gyr_curves["y"], gyry)
            set_curve(self.gyr_curves["z"], gyrz)
            if self.acc_now: self.acc_now.setPos(0.0)
            if self.gyr_now: self.gyr_now.setPos(0.0)
            x_half = half_window / 1000.0
            self.acc_plot.setXRange(-x_half, x_half, padding=0.0)
            self.gyr_plot.setXRange(-x_half, x_half, padding=0.0)
        finally:
            self._plotting = False

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.video_path:
            state = self._video_state_for(self.video_path)
            state["position_ms"] = int(self.player.position())
            state["sync_offset_ms"] = self.sync_offset_ms
        self.session_state["last_start_time_input"] = self.start_time_edit.text().strip()
        self.session_state["playback_rate"] = self._requested_playback_rate
        self._write_session_state(force=True)
        if self._imu_thread is not None and self._imu_thread.isRunning():
            QMessageBox.information(
                self,
                "Loading sensor files",
                "Please wait for the current sensor-folder load to finish before closing.",
            )
            event.ignore()
            return
        super().closeEvent(event)

def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
