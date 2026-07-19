import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pandas as pd
from PySide6.QtTest import QSignalSpy, QTest

import VideoLabeler_QT as labeler


class LoaderTests(unittest.TestCase):
    def test_invalid_iso_timestamp_is_removed(self):
        frame = pd.DataFrame(
            {
                "datetime": ["2026-01-01T00:00:00Z", "not-a-time"],
                "acc_x": [1.0, 2.0],
            }
        )
        values, source = labeler._timestamp_ms_series(frame)

        self.assertEqual(source, "datetime")
        self.assertTrue(np.isfinite(values.iloc[0]))
        self.assertTrue(np.isnan(values.iloc[1]))

    def test_headerless_loader_uses_expected_channel_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "imu.txt"
            path.write_text(
                "1,2,3,4,5,6,7,8,9,1751477000000\n"
                "2,3,4,5,6,7,8,9,10,1751477000010\n",
                encoding="utf-8",
            )

            frame = labeler._load_headerless_txt(str(path))

        self.assertEqual(len(frame), 2)
        self.assertEqual(frame["timestamp_ms"].tolist(), [1751477000000, 1751477000010])
        self.assertEqual(frame["acc_x"].tolist(), [1.0, 2.0])
        self.assertEqual(frame["gyr_z"].tolist(), [6.0, 7.0])
        self.assertEqual(frame["acc_x"].dtype, np.dtype("float32"))

    def test_folder_loader_sorts_only_when_needed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            pd.DataFrame(
                {
                    "timestamp_ms": [2000, 2010],
                    "acc_x": [2, 3],
                    "acc_y": [2, 3],
                    "acc_z": [2, 3],
                    "gyr_x": [2, 3],
                    "gyr_y": [2, 3],
                    "gyr_z": [2, 3],
                }
            ).to_csv(folder / "a.csv", index=False)
            pd.DataFrame(
                {
                    "timestamp_ms": [1000, 1010],
                    "acc_x": [0, 1],
                    "acc_y": [0, 1],
                    "acc_z": [0, 1],
                    "gyr_x": [0, 1],
                    "gyr_y": [0, 1],
                    "gyr_z": [0, 1],
                }
            ).to_csv(folder / "b.csv", index=False)

            frame = labeler.load_imu_folder(str(folder))

        self.assertEqual(frame["timestamp_ms"].tolist(), [1000, 1010, 2000, 2010])
        self.assertTrue(frame["timestamp_ms"].is_monotonic_increasing)


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = labeler.QApplication.instance() or labeler.QApplication([])

    def make_window(self, temp_dir: str):
        labeler.SESSION_FILE = str(Path(temp_dir) / "session_state.json")
        window = labeler.MainWindow()
        window.show()
        self.app.processEvents()
        return window

    def test_fine_sync_is_applied_to_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.make_window(temp_dir)
            window.video_start_utc_ms = 1_000_000
            window.sync_offset_ms = 125
            window.make_label("Stroke")

            self.assertEqual(window.labels[-1].imu_utc_ms, 1_000_125)
            window.close()

    def test_label_shortcut_is_ignored_while_editing_start_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.make_window(temp_dir)
            window.video_start_utc_ms = 1_000_000
            window.start_time_edit.setFocus()
            self.app.processEvents()

            window._run_shortcut(lambda: window.make_label("Turn"))

            self.assertEqual(window.labels, [])
            window.close()

    def test_repeated_save_does_not_duplicate_prior_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.make_window(temp_dir)
            window.video_path = str(Path(temp_dir) / "video.mp4")
            first = labeler.LabelEvent(10, 1010, "Stroke", None, 1)
            second = labeler.LabelEvent(20, 1020, "Turn", None, 2)

            window.labels.append(first)
            window.on_save_labels()
            window.labels.append(second)
            window.on_save_labels()

            rows = (Path(temp_dir) / labeler.LABELS_CSV).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 3)
            self.assertEqual(sum("Stroke" in row for row in rows), 1)
            self.assertEqual(sum("Turn" in row for row in rows), 1)
            window.close()

    def test_rapid_speed_changes_are_coalesced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.make_window(temp_dir)
            rate_changes = QSignalSpy(window.player.playbackRateChanged)

            window.on_speed_changed(150)
            window.on_speed_changed(200)
            window.on_speed_changed(250)

            self.assertTrue(window.speed_apply_timer.isActive())
            self.assertAlmostEqual(window.player.playbackRate(), 1.0)
            QTest.qWait(window._speed_apply_delay_ms + 50)

            self.assertAlmostEqual(window.player.playbackRate(), 2.5)
            self.assertEqual(rate_changes.count(), 1)
            self.assertEqual(window.session_state["playback_rate"], 2.5)
            window.close()

    def test_selected_speed_is_reapplied_after_media_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.make_window(temp_dir)
            window.on_speed_changed(200)
            window._apply_requested_playback_rate()
            window.player.setPlaybackRate(1.0)

            window.on_media_status_changed(labeler.QMediaPlayer.MediaStatus.LoadedMedia)

            self.assertAlmostEqual(window.player.playbackRate(), 2.0)
            window.close()

    def test_multi_hour_trace_is_bounded_to_display_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.make_window(temp_dir)
            sample_count = 3 * 60 * 60 * 100
            start_ms = 1_750_000_000_000
            timestamps = start_ms + np.arange(sample_count, dtype=np.int64) * 10
            base_signal = np.sin(np.arange(sample_count, dtype=np.float32) / 20)
            frame = pd.DataFrame(
                {
                    "acc_x": base_signal,
                    "acc_y": base_signal,
                    "acc_z": base_signal,
                    "gyr_x": base_signal,
                    "gyr_y": base_signal,
                    "gyr_z": base_signal,
                    "timestamp_ms": timestamps,
                }
            )
            window._on_imu_loaded(frame, temp_dir, 1)
            window.video_start_utc_ms = start_ms

            started = time.perf_counter()
            window.update_plots_for_position(90 * 60 * 1000)
            elapsed = time.perf_counter() - started

            plotted_points = len(window.acc_curves["x"].xData)
            self.assertLessEqual(plotted_points, 3000)
            self.assertLess(elapsed, 1.0)
            window.close()


if __name__ == "__main__":
    unittest.main()
