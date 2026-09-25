"""Train a bounded CatBoost experiment and evaluate the frozen winner on later dates."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time

from catboost import CatBoostRegressor
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .baseline import fit_baseline, metrics, predict_baseline
from .data import assign_splits
from .features import CATEGORICAL_FEATURES, FEATURES, build_features
from .pipeline import write_json

MODEL_LABELS = {
    "scheduled_on_time": "Always on time",
    "global_median": "Global median",
    "grouped_median": "Grouped median",
    "catboost": "CatBoost regression",
}


def split_experiment(frame, config):
    """Use separate boundaries without changing the first experiment's split labels."""
    labels = assign_splits(frame["scheduled_time_local"], config)
    selected = {}
    for name in ["train", "validation", "test"]:
        mask = labels.eq(name) & frame["valid_required_fields"]
        if name == "train":
            mask &= frame["baseline_eligible"]
        selected[name] = frame.loc[mask].sort_values(["scheduled_time_local", "row_id"]).copy()
        if selected[name].empty:
            raise ValueError(f"The {name} split has no usable observations.")
    for earlier, later in [("train", "validation"), ("validation", "test")]:
        if not selected[earlier]["scheduled_time_local"].max() < selected[later]["scheduled_time_local"].min():
            raise ValueError("Regression split dates overlap.")
        if set(selected[earlier]["row_id"]) & set(selected[later]["row_id"]):
            raise ValueError("Regression splits contain overlapping Row IDs.")
    return selected


def score_predictions(frame, predictions):
    result = {}
    for population in ["all_valid", "unflagged"]:
        mask = np.ones(len(frame), dtype=bool) if population == "all_valid" else frame["baseline_eligible"].to_numpy(dtype=bool)
        actual = frame.loc[mask, "delay_seconds"].to_numpy(dtype=float)
        result[population] = {name: metrics(actual, np.asarray(values)[mask]) for name, values in predictions.items()}
    return result


def prepare_candidates(training, validation, config, output_dir):
    """This function receives no test frame, so tuning cannot consult test labels."""
    x_train, x_val = build_features(training), build_features(validation)
    y_train = training["delay_seconds"].to_numpy(dtype=float)
    y_val = validation["delay_seconds"].to_numpy(dtype=float)
    candidates = []
    winner = None
    best_score = float("inf")
    for depth in config["candidate_depths"]:
        print(f"Training CatBoost depth={depth}; at most {config['iterations']} trees", flush=True)
        started = time.monotonic()
        model = CatBoostRegressor(
            loss_function="MAE", eval_metric="MAE", depth=depth,
            iterations=config["iterations"], learning_rate=config["learning_rate"],
            l2_leaf_reg=config["l2_leaf_reg"], random_seed=config["random_seed"],
            thread_count=config["thread_count"], allow_writing_files=False,
            # Categories are one-hot encoded, avoiding target-encoded category features.
            one_hot_max_size=255, counter_calc_method="SkipTest",
            cat_features=CATEGORICAL_FEATURES,
        )
        model.fit(x_train, y_train, eval_set=(x_val, y_val),
                  early_stopping_rounds=config["early_stopping_rounds"], use_best_model=True, verbose=100)
        values = model.predict(x_val, thread_count=config["thread_count"])
        score = metrics(y_val, values)
        info = {
            "depth": depth, "tree_count": model.tree_count_,
            "best_iteration_zero_based": model.get_best_iteration(),
            "training_seconds": time.monotonic() - started,
            "validation_all_valid": score, "learning_curve": model.get_evals_result(),
        }
        candidates.append(info)
        if score["mae_seconds"] < best_score:
            best_score = score["mae_seconds"]
            winner = model
        # Retain progress if a later fit is interrupted. Only the current winner is saved.
        winner.save_model(str(output_dir / "catboost.cbm"))
        write_json(output_dir / "selection_progress.json", candidates)
        print(f"Depth {depth}: validation MAE={score['mae_seconds']:.3f}s; trees={model.tree_count_}", flush=True)
    return winner, candidates


def plot_results(result, candidates, path):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.titleweight": "bold"})
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
    fig.suptitle("BLUE: regression versus historical medians\nAugust 31–September 5, 2026 · all valid observations, including flagged records", fontsize=17)
    test = result["test"]["all_valid"]
    names = list(MODEL_LABELS)
    colors = ["#AAB4C3", "#70839D", "#2674AF", "#168477"]
    for ax, key, scale, title, xlabel in [
        (axes[0, 0], "mae_seconds", 1, "Mean absolute error · lower is better", "Seconds"),
        (axes[0, 1], "within_120_seconds", 100, "Predictions within two minutes · higher is better", "Percent of observations"),
    ]:
        values = [test[name][key] * scale for name in names]
        bars = ax.barh([MODEL_LABELS[n] for n in names], values, color=colors, height=.58)
        ax.invert_yaxis()
        ax.set_title(title, loc="left", fontsize=11)
        ax.set_xlabel(xlabel)
        ax.set_xlim(0, max(values) * 1.2)
        for bar, value in zip(bars, values):
            ax.text(value + max(values) * .015, bar.get_y() + bar.get_height() / 2,
                    f"{value:.1f}{'%' if scale == 100 else ''}", va="center", fontsize=10)
    ax = axes[1, 0]
    daily = result["test_by_date"]
    dates = list(daily)
    for name, color in [("grouped_median", "#2674AF"), ("catboost", "#168477")]:
        ax.plot(range(len(dates)), [daily[d][name]["mae_seconds"] for d in dates],
                marker="o", label=MODEL_LABELS[name], color=color)
    ax.set_xticks(range(len(dates)), [pd.Timestamp(d).strftime("%b %d") for d in dates], rotation=25)
    ax.set_title("Error by test date", loc="left", fontsize=11)
    ax.set_ylabel("Mean absolute error (seconds)")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=.2)
    ax = axes[1, 1]
    for candidate, color in zip(candidates, ["#8055A1", "#B67526"]):
        curve = candidate["learning_curve"]["validation"]["MAE"]
        ax.plot(np.arange(1, len(curve) + 1), curve, color=color, label=f"Depth {candidate['depth']}")
        point = candidate["best_iteration_zero_based"]
        ax.scatter([point + 1], [curve[point]], color=color, s=28)
    ax.set_title("Validation learning curves · no test labels", loc="left", fontsize=11)
    ax.set_xlabel("Training iteration")
    ax.set_ylabel("Validation MAE (seconds)")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=.2)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def write_report(result, config, candidates, path):
    winner = result["selected_candidate"]
    g = result["test"]["all_valid"]["grouped_median"]
    m = result["test"]["all_valid"]["catboost"]
    difference = g["mae_seconds"] - m["mae_seconds"]
    verb = "reduced" if difference >= 0 else "increased"
    percentage = abs(difference) / g["mae_seconds"]
    lines = [
        "# BLUE regression experiment", "",
        f"On {m['n']:,} observed stop departures during August 31–September 5, CatBoost {verb} mean absolute error "
        f"by **{abs(difference):.1f} seconds ({percentage:.1%})** relative to a grouped-median baseline fit on exactly the same training rows.", "",
        "This is a comparison on a **new six-day test period**. Its scores are not directly comparable with the first report's August 24–30 scores.", "",
        "## Experiment design", "",
        "| Split | Local dates | Observations |",
        "|---|---|---:|",
        f"| Training, eligible only | July 25–August 23 | {result['counts']['train']:,} |",
        f"| Validation, all valid | August 24–30 | {result['counts']['validation']:,} |",
        f"| Test, all valid | August 31–September 5 | {result['counts']['test']:,} |", "",
        "The first experiment's test week is now explicitly used as validation. The new test starts after all development dates. "
        "September 6–9 have no BLUE observations, so the test ends before that gap; it includes Monday through Saturday and no Sunday. "
        "September 10–12 are not scored. Nonzero daily counts do not prove collection completeness.", "",
        "Both models fit the same eligible training observations. Primary validation and test scores include flagged extremes and duplicate candidates. "
        "Secondary scores exclude those flagged rows but use the same fitted models and predictions. No missing departures are generated or treated as cancellations.", "",
        "## What the regression model learns", "",
        "CatBoost is a model made from many decision trees. Unlike a fixed median lookup, it can learn combinations of stop, destination, "
        "day type, calendar weekday, and scheduled minute of day, and share patterns across similar observations.", "",
        "Inputs are exactly: `stop_number`, `destination`, `day_type`, `day_of_week`, and `minute_of_day`. "
        "Weekday and time-of-day features are derived from the scheduled local timestamp. Categorical values are passed as strings and "
        "one-hot encoded for this dataset. Row IDs, observed deviation, quality flags, split labels, and future/current bus delays are not features. "
        "The route is fixed to BLUE. No month effect is fitted from this short summer sample.", "",
        f"Two predeclared candidates (depths {config['candidate_depths']}) use MAE loss, learning rate {config['learning_rate']}, "
        f"at most {config['iterations']} trees, and seed {config['random_seed']}. Early stopping and candidate selection use validation MAE only. "
        f"The selected model has depth **{winner['depth']}** and **{winner['tree_count']} trees**. It is saved before the new test is scored; "
        "there is no refit on validation or test rows.", "",
        "| Candidate | Retained trees | Validation MAE (seconds) |",
        "|---|---:|---:|",
    ]
    for candidate in candidates:
        lines.append(f"| Depth {candidate['depth']} | {candidate['tree_count']} | {candidate['validation_all_valid']['mae_seconds']:.2f} |")
    lines += ["", "## New test results", "",
              "| Method | MAE, all valid | MAE, unflagged | Within ±2 min, all valid |",
              "|---|---:|---:|---:|"]
    for name, label in MODEL_LABELS.items():
        all_score, unflagged = result["test"]["all_valid"][name], result["test"]["unflagged"][name]
        lines.append(f"| {label} | {all_score['mae_seconds']:.2f} s | {unflagged['mae_seconds']:.2f} s | {all_score['within_120_seconds']:.1%} |")
    lines += ["", "Lower MAE means a smaller average prediction error. Within ±2 minutes is a prediction-error measure, not the percentage of buses running on time.", "",
              "## Differences by destination", "",
              "| Destination | Test observations | Grouped MAE | Regression MAE |",
              "|---|---:|---:|---:|"]
    for name, scores in result["test_by_destination"].items():
        lines.append(f"| {name} | {scores['catboost']['n']:,} | {scores['grouped_median']['mae_seconds']:.2f} s | {scores['catboost']['mae_seconds']:.2f} s |")
    lines += ["", "The metrics file also gives per-date results, tail errors, signed bias, and how many test categories were unseen in training. "
              "Rows can share a bus trip and are not independent samples. Six test dates are too few to establish robust seasonal generalization; "
              "no statistical-significance claim is made.", "",
              "## Interpretation and next step", "",
              "This predicts historical-pattern **departure delay**, not an arrival ETA or the evolving delay of a live bus. "
              "The CSV has no vehicle/trip IDs or stop sequence. Do not manufacture previous-stop delay with a row shift. "
              "After reviewing this result, use validation for further model choices and collect or reserve a new future period for another final test.", "",
              "The saved predictor can now be called with a stop, destination, day type, and scheduled time. "
              "A practical next improvement is collecting trip-linked live observations so the model can use the particular bus's recent delay.", "",
              "## Outputs and reproducibility", "",
              "- `../../models/regression/catboost.cbm`: selected model in CatBoost's native format.",
              "- `../../models/regression/metadata.json`: exact feature contract, training category vocabulary, selection details, and data hash.",
              "- `../../models/regression/grouped_median.json`: comparator fitted on the new training period.",
              "- `metrics.json`: full model comparisons, including per-date and per-destination scores.",
              "- `predictions.parquet`: row-level validation and test predictions for every comparison method.",
              "- `comparison.png`: aggregate, daily, and validation-learning plots.",
              "- `manifest.json`: configuration, software versions, artifact hashes, and checks.", "",
              "Model selection code receives only training and validation frames. Predictions from the saved model were checked against "
              "the in-memory model, and stored test predictions were independently reconciled with reported MAE.", "",
              "## References", "",
              "[CatBoost regressor](https://catboost.ai/docs/en/concepts/python-reference_catboostregressor), "
              "[validation and parameter tuning](https://catboost.ai/docs/en/concepts/parameter-tuning), "
              "[MAE objective](https://catboost.ai/docs/en/concepts/loss-functions-regression). "
              "Data provenance and parsing decisions are documented in `../first_baseline.md`.", ""]
    path.write_text("\n".join(lines))


def run(config_path):
    config_path = Path(config_path).resolve()
    root = config_path.parent
    config = json.loads(config_path.read_text())
    model_dir, report_dir = root / "models/regression", root / "reports/regression"
    model_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    data_path = root / config["prepared_data"]
    frame = pd.read_parquet(data_path)
    if set(frame["route_number"].dropna()) != {"BLUE"}:
        raise ValueError("This model is scoped to BLUE.")
    with data_path.open("rb") as stream:
        data_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    splits = split_experiment(frame, config)
    print("Observations by split:", {k: len(v) for k, v in splits.items()}, flush=True)
    # Record boundaries and candidates before training or evaluating the new holdout.
    write_json(report_dir / "experiment_plan.json", {"config": config, "features": FEATURES, "prepared_sha256": data_hash})
    baseline = fit_baseline(splits["train"], config["minimum_group_observations"])
    model, candidates = prepare_candidates(splits["train"], splits["validation"], config, model_dir)
    chosen = min(candidates, key=lambda item: item["validation_all_valid"]["mae_seconds"])
    vocabulary = {name: sorted(build_features(splits["train"])[name].unique().tolist()) for name in CATEGORICAL_FEATURES}
    metadata = {
        "route": "BLUE", "timezone": "America/Winnipeg", "target": "delay_seconds", "positive_means": "late",
        "features": FEATURES, "categorical_features": CATEGORICAL_FEATURES,
        "training_vocabulary": vocabulary, "config": config, "prepared_sha256": data_hash,
        "selected_depth": chosen["depth"], "selected_tree_count": chosen["tree_count"],
        "selection_metric": "validation MAE on all valid observations",
    }
    write_json(model_dir / "metadata.json", metadata)
    write_json(model_dir / "grouped_median.json", {
        "global_median": baseline["global_median"],
        "lookups": [{"keys": keys, "groups": table.to_dict(orient="records")} for keys, table in baseline["lookups"]],
    })
    # Selection is frozen. The saved artifact, rather than the live training object, is evaluated.
    loaded = CatBoostRegressor()
    loaded.load_model(str(model_dir / "catboost.cbm"))
    x_val = build_features(splits["validation"])
    np.testing.assert_allclose(model.predict(x_val), loaded.predict(x_val), rtol=0, atol=1e-10)
    result = {"counts": {name: len(part) for name, part in splits.items()},
              "selected_candidate": {k: v for k, v in chosen.items() if k != "learning_curve"}}
    records = []
    for name in ["validation", "test"]:
        part = splits[name]
        prediction, fallback = predict_baseline(baseline, part)
        predictions = {
            "scheduled_on_time": np.zeros(len(part)),
            "global_median": np.full(len(part), baseline["global_median"]),
            "grouped_median": prediction,
            "catboost": loaded.predict(build_features(part), thread_count=config["thread_count"]),
        }
        result[name] = score_predictions(part, predictions)
        record = part[["row_id", "stop_number", "destination", "day_type", "scheduled_time_local",
                       "delay_seconds", "baseline_eligible", "flag_extreme_delay", "flag_duplicate_candidate"]].copy()
        record["experiment_split"] = name
        record["median_fallback"] = fallback
        for model_name, values in predictions.items():
            if not np.isfinite(values).all():
                raise ValueError("Nonfinite model predictions.")
            record[f"prediction_{model_name}_seconds"] = values
        records.append(record)
    predictions = pd.concat(records, ignore_index=True)
    test_predictions = predictions.loc[predictions["experiment_split"].eq("test")]
    for column, key in [("destination", "test_by_destination"), ("scheduled_time_local", "test_by_date")]:
        grouping = test_predictions[column] if column == "destination" else test_predictions[column].dt.strftime("%Y-%m-%d")
        result[key] = {
            str(label): {name: metrics(part["delay_seconds"], part[f"prediction_{name}_seconds"])
                         for name in ["grouped_median", "catboost"]}
            for label, part in test_predictions.groupby(grouping, dropna=False)
        }
    test_features = build_features(splits["test"])
    result["test_unseen_category_rows"] = {name: int((~test_features[name].isin(vocabulary[name])).sum()) for name in CATEGORICAL_FEATURES}
    result["test_coverage"] = splits["test"].groupby(splits["test"]["scheduled_time_local"].dt.strftime("%Y-%m-%d")).size().to_dict()
    result["feature_importance_prediction_values_change"] = dict(zip(FEATURES, loaded.get_feature_importance().tolist()))
    predictions.to_parquet(report_dir / "predictions.parquet", index=False, compression="zstd")
    write_json(report_dir / "metrics.json", result)
    write_json(report_dir / "candidate_scores.json", candidates)
    write_report(result, config, candidates, report_dir / "report.md")
    plot_results(result, candidates, report_dir / "comparison.png")
    stored = pd.read_parquet(report_dir / "predictions.parquet")
    pd.testing.assert_frame_equal(stored, predictions)
    check = stored.loc[stored["experiment_split"].eq("test")]
    for name in MODEL_LABELS:
        mae = float((check[f"prediction_{name}_seconds"] - check["delay_seconds"]).abs().mean())
        np.testing.assert_allclose(mae, result["test"]["all_valid"][name]["mae_seconds"], rtol=0, atol=1e-10)
    with (model_dir / "catboost.cbm").open("rb") as stream:
        model_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    write_json(report_dir / "manifest.json", {
        "completed_at_utc": datetime.now(timezone.utc).isoformat(), "config": config,
        "prepared_sha256": data_hash, "model_sha256": model_hash,
        "dependencies": {name: importlib.metadata.version(name) for name in ["catboost", "pandas", "numpy", "pyarrow", "matplotlib"]},
        "checks": ["Chronological boundaries and Row IDs do not overlap", "Selection function receives no test data",
                   "Saved model reproduces validation predictions", "All predictions finite",
                   "Parquet round-trip preserves predictions", "Stored predictions reconcile with all test MAEs"],
    })
    print(json.dumps(result["test"], indent=2), flush=True)
    print(f"Completed regression experiment: {report_dir / 'report.md'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="regression_config.json")
    run(parser.parse_args().config)
