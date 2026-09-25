import json
from pathlib import Path
import tempfile
import unittest

from catboost import CatBoostRegressor
import numpy as np
import pandas as pd

from src.features import CATEGORICAL_FEATURES, FEATURES, build_features
from src.predict import predict_departure
from src.regression import score_predictions, split_experiment

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "regression_config.json").read_text())


def inputs():
    return pd.DataFrame({
        "stop_number": ["001", "002", "001", "002"] * 4,
        "destination": ["North", "South", "North", "South"] * 4,
        "day_type": ["Weekday"] * 16,
        "scheduled_time_local": pd.date_range("2026-08-01 08:00", periods=16, freq="h"),
    })


class RegressionTests(unittest.TestCase):
    def test_features_ignore_outcomes_flags_and_stale_derived_columns(self):
        frame = inputs()
        expected = build_features(frame)
        frame["delay_seconds"] = 999999
        frame["deviation_raw"] = "-999,999"
        frame["baseline_eligible"] = False
        frame["row_id"] = "spoof"
        frame["split"] = "test"
        frame["day_of_week"] = 999
        frame["hour"] = 999
        pd.testing.assert_frame_equal(build_features(frame), expected)
        self.assertEqual(expected.columns.tolist(), FEATURES)
        self.assertEqual(expected.iloc[0]["stop_number"], "001")

    def test_aware_time_is_converted_to_winnipeg_before_derivation(self):
        frame = inputs().iloc[:1].copy()
        frame["scheduled_time_local"] = pd.to_datetime(["2026-08-04T01:30:30Z"])
        values = build_features(frame).iloc[0]
        self.assertEqual(values["day_of_week"], "0")  # Monday locally, Tuesday in UTC.
        self.assertAlmostEqual(values["minute_of_day"], 20 * 60 + 30.5)

    def test_new_boundaries_override_old_splits_and_preserve_eval_flags(self):
        frame = pd.DataFrame({
            "row_id": ["1", "2", "3", "4", "5", "6"],
            "scheduled_time_local": pd.to_datetime([
                "2026-08-23 23:59:59", "2026-08-24 00:00:00", "2026-08-30 23:59:59",
                "2026-08-31 00:00:00", "2026-09-05 23:59:59", "2026-09-06 00:00:00",
            ]),
            "valid_required_fields": True, "baseline_eligible": [True, False, True, True, False, True],
            "split": "old_test",
        })
        groups = split_experiment(frame, CONFIG)
        self.assertEqual(groups["train"]["row_id"].tolist(), ["1"])
        self.assertEqual(groups["validation"]["row_id"].tolist(), ["2", "3"])
        self.assertEqual(groups["test"]["row_id"].tolist(), ["4", "5"])
        self.assertTrue(frame["split"].eq("old_test").all())

    def test_population_comparisons_use_same_predictions(self):
        frame = pd.DataFrame({"delay_seconds": [100, 5000], "baseline_eligible": [True, False]})
        scores = score_predictions(frame, {"model": np.array([80, 200])})
        self.assertEqual(scores["all_valid"]["model"]["mae_seconds"], 2410)
        self.assertEqual(scores["unflagged"]["model"]["mae_seconds"], 20)

    def test_saved_model_and_cli_predict_without_observed_delays(self):
        frame = inputs()
        features = build_features(frame)
        model = CatBoostRegressor(iterations=5, depth=2, loss_function="MAE", cat_features=CATEGORICAL_FEATURES,
                                 random_seed=42, thread_count=1, allow_writing_files=False, verbose=False)
        model.fit(features, np.arange(len(features)) * 10.0)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            model.save_model(str(directory / "catboost.cbm"))
            metadata = {"route": "BLUE", "timezone": "America/Winnipeg",
                        "training_vocabulary": {name: sorted(features[name].unique().tolist()) for name in CATEGORICAL_FEATURES}}
            (directory / "metadata.json").write_text(json.dumps(metadata))
            sample = frame.iloc[0]
            result = predict_departure(directory, sample.stop_number, sample.destination, sample.day_type, sample.scheduled_time_local)
            self.assertAlmostEqual(result["predicted_delay_seconds"], model.predict(features.iloc[:1])[0], places=2)
            self.assertEqual(result["stop_number"], "001")
            self.assertEqual(result["unseen_categories"], {})
            unseen = predict_departure(directory, "new_stop", "North", "Weekday", sample.scheduled_time_local)
            self.assertTrue(np.isfinite(unseen["predicted_delay_seconds"]))
            self.assertEqual(unseen["unseen_categories"]["stop_number"], "new_stop")


if __name__ == "__main__":
    unittest.main()
