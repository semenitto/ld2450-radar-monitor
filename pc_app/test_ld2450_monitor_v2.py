import tempfile
import time
import unittest
from pathlib import Path

from ld2450_monitor_v2 import (
    CalibrationSettings,
    HistorySample,
    HistoryStore,
    Target,
    ZoneRect,
    _windows_colorref,
    load_calibration,
    migrate_legacy_history,
    parse_data_line,
    person_count_text,
    save_calibration,
    transform_target,
    zone_index_for_point,
    zone_index_for_target,
)


class MonitorLogicTests(unittest.TestCase):
    def test_windows_colorref_conversion(self):
        self.assertEqual(_windows_colorref("#123456"), 0x563412)

    def test_parse_frame(self):
        line = "LD2450_DATA,1000,42,1,-500,2000,12,60,1,1200,3200,-5,60,0,0,0,0,0"
        frame = parse_data_line(line)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.counter, 42)
        self.assertTrue(frame.targets[0].valid)
        self.assertEqual(frame.targets[0].x, -500)
        self.assertFalse(frame.targets[2].valid)

    def test_zone_mapping(self):
        self.assertEqual(zone_index_for_target(Target(True, -2500, 3000, 0, 0)), 0)
        self.assertEqual(zone_index_for_target(Target(True, 0, 3000, 0, 0)), 1)
        self.assertEqual(zone_index_for_target(Target(True, 2500, 3000, 0, 0)), 2)
        self.assertIsNone(zone_index_for_target(Target(False, 0, 3000, 0, 0)))

    def test_room_transform_rotation_and_mirror(self):
        settings = CalibrationSettings(radar_x=1.0, radar_y=0.5, rotation_deg=90)
        x, y = transform_target(Target(True, 0, 2000, 0, 0), settings)
        self.assertAlmostEqual(x, -1.0, places=5)
        self.assertAlmostEqual(y, 0.5, places=5)
        settings.mirror_x = True
        x, _ = transform_target(Target(True, 1000, 0, 0, 0), settings)
        self.assertAlmostEqual(x, 1.0, places=5)

    def test_rectangular_zone_hysteresis(self):
        settings = CalibrationSettings(
            hysteresis=0.15,
            zones=[
                ZoneRect("A", -2.0, 0.0, -1.0, 1.0),
                ZoneRect("B", -0.5, 2.0, 0.5, 4.0),
                ZoneRect("C", 1.0, 0.0, 2.0, 2.0),
            ],
        )
        self.assertEqual(zone_index_for_point(0.0, 3.0, settings), 1)
        self.assertEqual(zone_index_for_point(0.58, 3.0, settings, previous_zone=1), 1)
        self.assertIsNone(zone_index_for_point(0.70, 3.0, settings, previous_zone=1))

    def test_calibration_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            settings = CalibrationSettings(room_width=7.5, mirror_x=True, smoothing=0.4)
            save_calibration(path, settings)
            restored = load_calibration(path)
            self.assertEqual(restored.room_width, 7.5)
            self.assertTrue(restored.mirror_x)
            self.assertEqual(len(restored.zones), 3)

    def test_history_round_trip_and_stats(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(Path(temp_dir) / "history.db")
            now = int(time.time())
            samples = [
                HistorySample(now - 2, 0, 2.0, (0, 0, 0)),
                HistorySample(now - 1, 1, 18.0, (0, 1, 0)),
                HistorySample(now, 2, 74.0, (0, 1, 1)),
            ]
            store.append_many(samples)
            self.assertEqual(store.query(now - 3, now), samples)
            occupancy, zones, recorded = store.today_stats(now)
            self.assertEqual(occupancy[:3], [1, 1, 1])
            self.assertEqual(zones, [0, 2, 1])
            self.assertEqual(recorded, 3)
            store.close()

    def test_legacy_history_keeps_occupancy_but_resets_zones(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy = HistoryStore(Path(temp_dir) / "history.db")
            now = int(time.time())
            legacy.append(HistorySample(now, 2, 50.0, (1, 1, 0)))
            legacy.close()
            destination = HistoryStore(Path(temp_dir) / "history_rect.db")
            migrate_legacy_history(Path(temp_dir) / "history.db", destination)
            self.assertEqual(
                destination.query(now, now),
                [HistorySample(now, 2, 50.0, (0, 0, 0))],
            )
            destination.close()

    def test_russian_person_forms(self):
        self.assertEqual(person_count_text(0), "0 человек")
        self.assertEqual(person_count_text(1), "1 человек")
        self.assertEqual(person_count_text(2), "2 человека")


if __name__ == "__main__":
    unittest.main()
