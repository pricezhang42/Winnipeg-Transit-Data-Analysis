import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from src.baseline import evaluate, fit_baseline, metrics, predict_baseline
from src.data import COLUMNS, assign_splits, parse_deviation, prepare_route

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


def raw_frame(rows):
    defaults = {
        "Row ID": "0001", "Stop Number": "06001", "Route Number": "BLUE",
        "Route Name": "BLUE", "Route Destination": "Downtown", "Day Type": "Weekday",
        "Scheduled Time": "2026 Aug 10 08:00:00 AM", "Deviation": "-60",
        "Location": "POINT (-97.15 49.89)",
    }
    frame = pd.DataFrame([{**defaults, **row} for row in rows], dtype="string")
    frame["source_record_number"] = np.arange(1, len(frame) + 1)
    return frame


class ParsingTests(unittest.TestCase):
    def test_thousands_and_sign_without_accepting_malformed_numbers(self):
        raw = pd.Series(["-1,063", "1,200", "0", " +42 ", "-1,06", "1,2,3", "12.5", None], dtype="string")
        result = parse_deviation(raw)
        self.assertEqual(result.iloc[:4].tolist(), [-1063, 1200, 0, 42])
        self.assertTrue(result.iloc[4:].isna().all())
        frame = prepare_route(raw_frame([{"Deviation": "-1,063"}]), CONFIG)
        self.assertEqual(frame.loc[0, "delay_seconds"], 1063)
        self.assertEqual(frame.loc[0, "stop_number"], "06001")
        self.assertEqual(frame.loc[0, "row_id"], "0001")

    def test_duplicate_and_extreme_rows_are_preserved_with_provenance(self):
        frame = prepare_route(raw_frame([
            {"Row ID": "001"}, {"Row ID": "002"},
            {"Row ID": "003", "Deviation": "5,000"},
        ]), CONFIG)
        self.assertEqual(len(frame), 3)
        duplicate = frame.loc[frame["row_id"].eq("002")].iloc[0]
        self.assertTrue(duplicate["flag_duplicate_candidate"])
        self.assertEqual(duplicate["duplicate_of_row_id"], "001")
        extreme = frame.loc[frame["row_id"].eq("003")].iloc[0]
        self.assertEqual(extreme["delay_seconds"], -5000)
        self.assertTrue(extreme["flag_extreme_delay"])
        self.assertFalse(extreme["baseline_eligible"])
        self.assertEqual(frame["baseline_eligible"].sum(), 1)

    def test_conflicting_row_ids_fail_instead_of_silent_deduplication(self):
        with self.assertRaisesRegex(ValueError, "Conflicting records"):
            prepare_route(raw_frame([{"Deviation": "-10"}, {"Deviation": "-20"}]), CONFIG)

    def test_invalid_time_target_and_coordinates_have_separate_flags(self):
        frame = prepare_route(raw_frame([
            {"Row ID": "01", "Scheduled Time": "broken"},
            {"Row ID": "02", "Deviation": "-1,06"},
            {"Row ID": "03", "Location": "POINT (500 95)"},
        ]), CONFIG).set_index("row_id")
        self.assertFalse(frame.loc["01", "valid_required_fields"])
        self.assertFalse(frame.loc["02", "valid_required_fields"])
        self.assertTrue(frame.loc["03", "flag_invalid_location"])
        self.assertTrue(frame.loc["03", "baseline_eligible"])

    def test_split_boundaries_and_midnight_are_not_shuffled(self):
        times = pd.Series(pd.to_datetime([
            "2026-08-16 23:59:59", "2026-08-17 00:00:00", "2026-08-23 23:59:59",
            "2026-08-24 00:00:00", "2026-08-30 23:59:59", "2026-08-31 00:00:00", None,
        ]))
        self.assertEqual(assign_splits(times, CONFIG).tolist(),
                         ["train", "validation", "validation", "test", "test", "later", "invalid"])
        bad = {**CONFIG, "test_start": CONFIG["validation_start"]}
        with self.assertRaises(ValueError):
            assign_splits(times, bad)

    def test_parquet_roundtrip_retains_raw_fields_identifiers_and_flags(self):
        frame = prepare_route(raw_frame([{"Deviation": "-1,063"}]), CONFIG)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prepared.parquet"
            frame.to_parquet(path, index=False)
            recovered = pd.read_parquet(path)
        pd.testing.assert_frame_equal(frame, recovered)
        self.assertTrue(set(COLUMNS.values()) <= set(recovered.columns))


class BaselineTests(unittest.TestCase):
    def test_sparse_and_unseen_groups_have_training_only_fallbacks(self):
        training = pd.DataFrame({
            "stop_number": ["A", "A", "B", "B"], "destination": ["North"] * 4,
            "day_type": ["Weekday"] * 4, "hour": [8, 9, 8, 8],
            "delay_seconds": [10, 30, 80, 120],
        })
        model = fit_baseline(training, minimum_count=2)
        queries = pd.DataFrame({
            "stop_number": ["A", "B", "C", "C"],
            "destination": ["North", "North", "North", "South"],
            "day_type": ["Weekday"] * 4, "hour": [8, 8, 9, 9],
        })
        values, levels = predict_baseline(model, queries)
        np.testing.assert_allclose(values, [20, 100, 55, 55])
        self.assertEqual(levels[0], "stop_number+destination+day_type")
        self.assertEqual(levels[1], "stop_number+destination+day_type+hour")
        self.assertEqual(levels[-1], "global_median")
        # An arbitrary caller index cannot reorder or misalign the forecasts.
        queries.index = [10, 4, 8, 2]
        np.testing.assert_allclose(predict_baseline(model, queries)[0], values)

    def test_evaluation_targets_do_not_change_model_or_predictions(self):
        frame = prepare_route(raw_frame([
            {"Row ID": "01", "Deviation": "-30"},
            {"Row ID": "02", "Deviation": "-50", "Scheduled Time": "2026 Aug 11 08:00:00 AM"},
            {"Row ID": "03", "Deviation": "-100", "Scheduled Time": "2026 Aug 18 08:00:00 AM"},
            {"Row ID": "04", "Deviation": "-200", "Scheduled Time": "2026 Aug 25 08:00:00 AM"},
            {"Row ID": "05", "Deviation": "-8,000", "Scheduled Time": "2026 Aug 25 08:05:00 AM"},
        ]), CONFIG)
        scores, predictions, model = evaluate(frame, minimum_count=1)
        self.assertEqual(model["global_median"], 40)
        np.testing.assert_allclose(predictions["prediction_seconds"], [40, 40, 40])
        self.assertEqual(scores["scores"]["test"]["all_valid"]["grouped_median"]["n"], 2)
        self.assertEqual(scores["scores"]["test"]["unflagged"]["grouped_median"]["n"], 1)
        edited = frame.copy()
        edited.loc[edited["split"].isin(["validation", "test"]), "delay_seconds"] += 12345
        _, changed, changed_model = evaluate(edited, minimum_count=1)
        self.assertEqual(changed_model["global_median"], model["global_median"])
        np.testing.assert_array_equal(changed["prediction_seconds"], predictions["prediction_seconds"])

    def test_metrics_have_known_units_and_tolerances(self):
        result = metrics([0, 60, 180], [0, 0, 0])
        self.assertEqual(result["mae_seconds"], 80)
        self.assertEqual(result["median_absolute_error_seconds"], 60)
        self.assertAlmostEqual(result["within_60_seconds"], 2 / 3)
        self.assertAlmostEqual(result["p90_absolute_error_seconds"], 156)
        with self.assertRaises(ValueError):
            metrics([np.nan], [0])


if __name__ == "__main__":
    unittest.main()
