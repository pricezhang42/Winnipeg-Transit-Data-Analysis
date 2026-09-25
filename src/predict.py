"""Predict a BLUE departure delay using the locally saved regression model."""

import argparse
import json
from pathlib import Path

from catboost import CatBoostRegressor
import pandas as pd

from .features import build_features


def predict_departure(model_dir, stop_number, destination, day_type, scheduled_time):
    model_dir = Path(model_dir)
    metadata = json.loads((model_dir / "metadata.json").read_text())
    if metadata.get("route") != "BLUE":
        raise ValueError("Expected the BLUE model.")
    time = pd.Timestamp(scheduled_time)
    if pd.isna(time):
        raise ValueError("A valid scheduled time is required.")
    if time.tzinfo is not None:
        time = time.tz_convert(metadata["timezone"]).tz_localize(None)
    row = pd.DataFrame([{
        "stop_number": str(stop_number), "destination": destination, "day_type": day_type,
        "scheduled_time_local": time,
    }])
    features = build_features(row)
    model = CatBoostRegressor()
    model.load_model(str(model_dir / "catboost.cbm"))
    if model.feature_names_ != features.columns.tolist():
        raise ValueError("Saved model feature contract differs from the current predictor.")
    delay = float(model.predict(features)[0])
    unseen = {name: features.iloc[0][name] for name, known in metadata["training_vocabulary"].items()
              if features.iloc[0][name] not in known}
    return {
        "route": "BLUE", "stop_number": str(stop_number), "destination": destination,
        "day_type": day_type, "timezone": metadata["timezone"],
        "scheduled_departure_local": time.isoformat(),
        "predicted_delay_seconds": round(delay, 2),
        "predicted_departure_local": (time + pd.Timedelta(seconds=delay)).round("s").isoformat(),
        "positive_delay_means": "late", "unseen_categories": unseen,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default=str(Path(__file__).resolve().parents[1] / "models/regression"))
    parser.add_argument("--stop", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--day-type", required=True, choices=["Weekday", "Saturday", "Sunday", "Holiday"])
    parser.add_argument("--scheduled-time", required=True, help="ISO scheduled departure; interpreted in America/Winnipeg")
    args = parser.parse_args()
    print(json.dumps(predict_departure(args.model_dir, args.stop, args.destination, args.day_type, args.scheduled_time), indent=2))
