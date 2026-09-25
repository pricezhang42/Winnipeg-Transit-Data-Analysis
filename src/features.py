"""Prediction-time features only; observed delays and quality flags never enter X."""

import numpy as np
import pandas as pd

CATEGORICAL_FEATURES = ["stop_number", "destination", "day_type", "day_of_week"]
NUMERIC_FEATURES = ["minute_of_day"]
FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES
MISSING_CATEGORY = "__MISSING__"


def build_features(frame):
    required = ["stop_number", "destination", "day_type", "scheduled_time_local"]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing prediction inputs: {sorted(missing)}")
    times = pd.to_datetime(frame["scheduled_time_local"], errors="coerce")
    if times.isna().any():
        raise ValueError("Every prediction needs a valid scheduled time.")
    if times.dt.tz is not None:
        times = times.dt.tz_convert("America/Winnipeg").dt.tz_localize(None)
    result = pd.DataFrame(index=frame.index)
    for name in ["stop_number", "destination", "day_type"]:
        result[name] = frame[name].astype("string").fillna(MISSING_CATEGORY).astype(str)
    result["day_of_week"] = times.dt.dayofweek.astype(str)
    result["minute_of_day"] = (
        times.dt.hour * 60 + times.dt.minute + times.dt.second / 60
    ).astype(float)
    if not np.isfinite(result["minute_of_day"]).all():
        raise ValueError("Scheduled minute must be finite.")
    return result[FEATURES]
