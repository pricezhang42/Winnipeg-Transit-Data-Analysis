"""Training-only median lookups with explicit fallbacks and frozen evaluation."""

import numpy as np
import pandas as pd

GROUP_LEVELS = [
    ["stop_number", "destination", "day_type", "hour"],
    ["stop_number", "destination", "day_type"],
    ["stop_number", "destination"],
    ["destination", "day_type", "hour"],
    ["destination", "day_type"],
    ["destination"],
]


def fit_baseline(training, minimum_count=20):
    if training.empty:
        raise ValueError("No eligible training observations.")
    if minimum_count < 1:
        raise ValueError("minimum_count must be positive.")
    lookups = []
    for keys in GROUP_LEVELS:
        table = training.groupby(keys, observed=True)["delay_seconds"].agg(["median", "count"])
        lookups.append((keys, table.loc[table["count"].ge(minimum_count)].reset_index()))
    return {"global_median": float(training["delay_seconds"].median()), "lookups": lookups}


def predict_baseline(model, features):
    """Only grouping features enter the lookup; evaluation targets are never read."""
    predictions = np.full(len(features), np.nan)
    levels = np.full(len(features), "global_median", dtype=object)
    for keys, table in model["lookups"]:
        if table.empty:
            continue
        matches = features[keys].merge(table, on=keys, how="left", sort=False, validate="many_to_one")
        values = matches["median"].to_numpy(dtype=float, na_value=np.nan)
        take = np.isnan(predictions) & ~np.isnan(values)
        predictions[take] = values[take]
        levels[take] = "+".join(keys)
    predictions[np.isnan(predictions)] = model["global_median"]
    return predictions, levels


def metrics(actual, predicted):
    actual, predicted = np.asarray(actual, dtype=float), np.asarray(predicted, dtype=float)
    if len(actual) == 0:
        return {"n": 0}
    if not np.isfinite(actual).all() or not np.isfinite(predicted).all():
        raise ValueError("Metrics require finite observations and predictions.")
    error = predicted - actual
    absolute = np.abs(error)
    return {
        "n": len(actual), "mae_seconds": float(absolute.mean()),
        "rmse_seconds": float(np.sqrt(np.mean(error ** 2))),
        "median_absolute_error_seconds": float(np.median(absolute)),
        "p90_absolute_error_seconds": float(np.quantile(absolute, .9)),
        "mean_signed_error_seconds": float(error.mean()),
        "within_60_seconds": float((absolute <= 60).mean()),
        "within_120_seconds": float((absolute <= 120).mean()),
        "within_180_seconds": float((absolute <= 180).mean()),
    }


def evaluate(frame, minimum_count):
    training = frame.loc[frame["split"].eq("train") & frame["baseline_eligible"]]
    model = fit_baseline(training, minimum_count)
    evaluation = frame.loc[frame["split"].isin(["validation", "test"]) & frame["valid_required_fields"]].copy()
    if set(evaluation["split"]) != {"validation", "test"}:
        raise ValueError("Both validation and test must contain valid observations.")
    if not training["scheduled_time_local"].max() < evaluation["scheduled_time_local"].min():
        raise ValueError("Training and evaluation dates overlap.")
    values, levels = predict_baseline(model, evaluation)
    evaluation["prediction_seconds"] = values
    evaluation["fallback_level"] = levels
    evaluation["absolute_error_seconds"] = np.abs(values - evaluation["delay_seconds"].to_numpy(dtype=float))
    result = {"training_rows": len(training), "training_global_median_seconds": model["global_median"], "scores": {}}
    for split in ["validation", "test"]:
        result["scores"][split] = {}
        for population in ["all_valid", "unflagged"]:
            subset = evaluation.loc[evaluation["split"].eq(split)]
            if population == "unflagged":
                subset = subset.loc[subset["baseline_eligible"]]
            actual = subset["delay_seconds"].to_numpy(dtype=float)
            result["scores"][split][population] = {
                "scheduled_on_time": metrics(actual, np.zeros(len(subset))),
                "training_global_median": metrics(actual, np.full(len(subset), model["global_median"])),
                "grouped_median": metrics(actual, subset["prediction_seconds"]),
                "fallback_counts": subset["fallback_level"].value_counts().to_dict(),
            }
    result["test_by_destination"] = {
        str(name): metrics(group["delay_seconds"], group["prediction_seconds"])
        for name, group in evaluation.loc[evaluation["split"].eq("test")].groupby("destination", dropna=False)
    }
    result["test_by_date"] = {
        date.strftime("%Y-%m-%d"): metrics(group["delay_seconds"], group["prediction_seconds"])
        for date, group in evaluation.loc[evaluation["split"].eq("test")].groupby("scheduled_date")
    }
    columns = ["row_id", "stop_number", "destination", "day_type", "scheduled_time_local",
               "delay_seconds", "prediction_seconds", "absolute_error_seconds", "fallback_level",
               "split", "baseline_eligible", "flag_extreme_delay", "flag_duplicate_candidate"]
    return result, evaluation[columns], model
