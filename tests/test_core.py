import os
import tempfile
import unittest
import zipfile

import numpy as np

from motodata.catalog import infer_unit_from
from motodata import discovery
from motodata.lapdata import LapData, _distance_axis
from motodata.pickers import lap_candidates
from motodata.reader import Lap, LapInfo


def make_lap(root, lap_time, distance, channels):
    lap_dir = os.path.join(root, "Lap_1")
    os.makedirs(lap_dir)
    ztx = os.path.join(lap_dir, "FlashData.ztx")
    with zipfile.ZipFile(ztx, "w") as z:
        for name, values in channels.items():
            z.writestr(name + ".sar", np.asarray(values, "<f8").tobytes())
    with open(os.path.join(lap_dir, "LapHeader.xml"), "w", encoding="utf-8") as f:
        f.write(f"<Lap><LapTime>{lap_time}</LapTime><LapDistance>{distance}</LapDistance></Lap>")
    return ztx


class DistanceTests(unittest.TestCase):
    def test_wrap_uses_lap_length(self):
        raw = np.array([90.0, 97.0, 4.0, 12.0])
        np.testing.assert_allclose(_distance_axis(raw, 100.0), [0, 7, 14, 22])
        np.testing.assert_allclose(raw, [90, 97, 4, 12])

    def test_invalid_values_and_noise(self):
        raw = np.array([10.0, np.nan, 20.0, 19.0, 30.0])
        np.testing.assert_allclose(_distance_axis(raw, 100.0), [0, 5, 10, 10, 20])
        raw = np.array([90.0, 97.0, np.nan, 4.0, 12.0])
        np.testing.assert_allclose(_distance_axis(raw, 100.0), [0, 7, 10.5, 14, 22])
        self.assertEqual(len(_distance_axis(np.array([np.nan]), 100.0)), 0)

    def test_wrap_without_lap_length_is_rejected(self):
        raw = np.array([900.0, 950.0, 0.0, 50.0])
        self.assertEqual(len(_distance_axis(raw)), 0)


class ReaderTests(unittest.TestCase):
    def test_values_are_not_globally_decoded(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 3, 100, {"nGear": [1, 2, 6]})
            with Lap(ztx) as lap:
                np.testing.assert_array_equal(lap.channel("nGear")[1], [1, 2, 6])

    def test_read_does_not_change_telemetry(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"vCar": [10, 20]})
            files = (ztx, os.path.join(os.path.dirname(ztx), "LapHeader.xml"))
            before = {}
            for path in files:
                with open(path, "rb") as f:
                    before[path] = os.stat(path).st_mtime_ns, f.read()
            with Lap(ztx) as lap:
                lap.channel("vCar")
            after = {}
            for path in files:
                with open(path, "rb") as f:
                    after[path] = os.stat(path).st_mtime_ns, f.read()
            self.assertEqual(before, after)

    def test_missing_samples_span_the_lap(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 10, 100, {"vCar": np.arange(19)})
            with Lap(ztx) as lap:
                t, _ = lap.channel("vCar")
                np.testing.assert_allclose(t, np.arange(19) * 10 / 19)
                self.assertEqual(lap.rate_snapped("vCar"), 2)
                self.assertFalse(t.flags.writeable)

    def test_markers_ignore_case(self):
        for marker in ("In", "in", "OUT", "box"):
            info = LapInfo("", "", 1, marker, None, None, None)
            self.assertFalse(info.is_flying)


class CacheTests(unittest.TestCase):
    def test_only_retained_channels_stay_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"aOne": [1, 2], "aTwo": [3, 4], "aThree": [5, 6, 7]})
            lap = LapData(ztx, 2, lap_distance=20)
            try:
                lap.xy("aOne", "time")
                lap.xy("aTwo", "time")
                lap.xy("aThree", "time")
                lap.retain_channels(["aTwo"])
                self.assertEqual(set(lap._cache), {"aTwo"})
                self.assertFalse(lap._xcache)
                self.assertEqual(set(lap.lap._time_axes), {2})
            finally:
                lap.close()

    def test_malformed_channel_is_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"vCar": [10, 20]})
            with zipfile.ZipFile(ztx, "a") as z:
                z.writestr("bad.sar", b"bad")
            lap = LapData(ztx, 2, lap_distance=20)
            try:
                self.assertEqual(len(lap.ty("bad")[0]), 0)
                self.assertTrue(lap.channel_error("bad"))
            finally:
                lap.close()

    def test_empty_distance_falls_back_to_speed(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"sLap": [], "vCar": [36, 36, 36, 36]})
            lap = LapData(ztx, 2, lap_distance=20)
            try:
                self.assertTrue(lap.has_distance)
                self.assertAlmostEqual(lap.dist_max(), 20)
            finally:
                lap.close()

    def test_alternate_speed_channel_builds_distance(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"CarSpd_vCar": [36, 36, 36, 36]})
            with LapData(ztx, 2, lap_distance=20) as lap:
                self.assertTrue(lap.has_distance)
                self.assertAlmostEqual(lap.dist_max(), 20)

    def test_stationary_speed_has_no_distance(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"vCar": [0, 0, 0, 0]})
            with LapData(ztx, 2, lap_distance=20) as lap:
                self.assertFalse(lap.has_distance)

    def test_partial_distance_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 100, {"sLap": [0, 20, 40, 50]})
            lap = LapData(ztx, 2, lap_distance=100)
            try:
                self.assertFalse(lap.has_distance)
            finally:
                lap.close()

    def test_inverse_distance_keeps_stationary_time(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 4, 20, {"sLap": [0, 10, 10, 20]})
            lap = LapData(ztx, 4, lap_distance=20)
            try:
                self.assertEqual(lap.to_time(10), 2)
                self.assertEqual(lap.to_time(20), 4)
            finally:
                lap.close()

    def test_distance_endpoint_uses_full_lap_time(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 3, 100, {"sLap": [0, 50, 100.2]})
            with LapData(ztx, 3, lap_distance=100) as lap:
                self.assertAlmostEqual(lap.to_time(lap.dist_max()), 3)

    def test_distance_axis_is_shared_by_sample_count(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {
                "sLap": [0, 10, 20, 20], "aOne": [1, 2, 3], "aTwo": [4, 5, 6]
            })
            with LapData(ztx, 2, lap_distance=20) as lap:
                self.assertIs(lap.x("aOne", "dist"), lap.x("aTwo", "dist"))

    def test_nonfinite_value_is_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"aOne": [np.nan, np.nan]})
            with LapData(ztx, 2, lap_distance=20) as lap:
                self.assertIsNone(lap.value_at("aOne", 1, "time"))


class SelectionTests(unittest.TestCase):
    def test_candidates_exclude_marked_and_short_laps(self):
        rows = [
            {"dir": "out", "lt": 20.0, "mk": "OUT", "dist": 5000},
            {"dir": "short", "lt": 30.0, "mk": "", "dist": 1000},
            {"dir": "short2", "lt": 31.0, "mk": "", "dist": 1100},
            {"dir": "fast", "lt": 90.0, "mk": "", "dist": 5000},
            {"dir": "next", "lt": 91.0, "mk": None, "dist": 5000},
        ]
        self.assertEqual([r["dir"] for r in lap_candidates(rows)], ["fast", "next"])

    def test_candidates_keep_missing_distance_and_drop_outlier(self):
        rows = [
            {"dir": "missing", "lt": 89.0, "mk": "", "dist": None},
            {"dir": "fast", "lt": 90.0, "mk": "", "dist": 5000},
            {"dir": "next", "lt": 91.0, "mk": "", "dist": 5010},
            {"dir": "corrupt", "lt": 92.0, "mk": "", "dist": 12000},
        ]
        self.assertEqual([r["dir"] for r in lap_candidates(rows)],
                         ["missing", "fast", "next"])

    def test_two_conflicting_distances_keep_both_laps(self):
        rows = [
            {"dir": "valid", "lt": 90.0, "mk": "", "dist": 5000},
            {"dir": "corrupt", "lt": 92.0, "mk": "", "dist": 12000},
        ]
        self.assertEqual([r["dir"] for r in lap_candidates(rows)],
                         ["valid", "corrupt"])

    def test_removed_outlier_stays_removed(self):
        rows = [
            {"dir": "short", "lt": 30.0, "mk": "", "dist": 1000},
            {"dir": "good", "lt": 90.0, "mk": "", "dist": 5000},
            {"dir": "outlier", "lt": 92.0, "mk": "", "dist": 12000},
        ]
        self.assertEqual([r["dir"] for r in lap_candidates(rows)], ["good"])

    def test_boolean_names_override_quantity_words(self):
        self.assertEqual(infer_unit_from("In_b_pAmbient_inError", "Ambient pressure error")[0], "")
        self.assertEqual(infer_unit_from("pAmbient", "Ambient pressure")[0], "bar")
        self.assertEqual(infer_unit_from("nTurbo", "Turbo speed elaborated value")[0], None)
        self.assertEqual(infer_unit_from("Rot_TC_Pos", "Traction control rotary position")[0], None)
        self.assertEqual(infer_unit_from("GPS_Latitude", "Latitude from GPS")[0], "deg")
        self.assertEqual(infer_unit_from(
            "Vpara_nTrackDetManAddLatDisp", "Lateral coordinate of a GPS point")[0], None)
        self.assertNotEqual(infer_unit_from(
            "LbdTgt_tAirComp", "Lambda target compensation f(tAir)")[0], "C")


class DiscoveryTests(unittest.TestCase):
    def test_cap_uses_sorted_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            for branch in ("a", "b"):
                lap = os.path.join(root, branch, "Lap_1")
                os.makedirs(lap)
                with open(os.path.join(lap, "FlashData.ztx"), "wb"):
                    pass
            found = discovery.find_lap_dirs(root, cap=1)
            self.assertEqual(os.path.basename(os.path.dirname(found[0])), "a")

    def test_directory_work_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            for branch in ("a", "b", "c"):
                os.makedirs(os.path.join(root, branch))
            with self.assertRaises(discovery.ScanLimitError):
                discovery.find_lap_dirs(root, directory_cap=2)

    def test_original_lap_meta_shape_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            ztx = make_lap(root, 2, 20, {"vCar": [10, 20]})
            lap_dir = os.path.dirname(ztx)
            self.assertEqual(discovery.lap_meta(lap_dir, {}), (2.0, None))
            self.assertEqual(discovery.lap_header_meta(lap_dir, {}), (2.0, None, 20.0))


if __name__ == "__main__":
    unittest.main()
