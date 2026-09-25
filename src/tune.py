"""Small rolling-origin comparison; holdout labels are used only after selection."""

import argparse
from datetime import datetime, timezone
import hashlib
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
from .features import CATEGORICAL_FEATURES, FEATURES, build_features
from .pipeline import write_json
from .regression import score_predictions


def validate_config(config):
    start = pd.Timestamp(config["train_start"])
    train_end = pd.Timestamp(config["final_training_end_exclusive"])
    holdout = pd.Timestamp(config["holdout_start"])
    end = pd.Timestamp(config["holdout_end_exclusive"])
    if not start < train_end <= holdout < end:
        raise ValueError("Invalid training and holdout boundaries.")
    previous_end = start
    for fold in config["folds"]:
        a, b = pd.Timestamp(fold["validation_start"]), pd.Timestamp(fold["validation_end_exclusive"])
        if not start < a < b <= train_end or a < previous_end:
            raise ValueError("Validation windows must be chronological, disjoint, and before the holdout.")
        previous_end = b
    names = [c["name"] for c in config["candidates"]]
    if len(names) != len(set(names)) or "reference" not in names:
        raise ValueError("Candidate names must be unique and include reference.")


def fold_frames(development, config, fold):
    times = development["scheduled_time_local"]
    train = development.loc[times.ge(config["train_start"]) & times.lt(fold["validation_start"])
                            & development["baseline_eligible"]]
    validation = development.loc[times.ge(fold["validation_start"]) & times.lt(fold["validation_end_exclusive"])
                                 & development["valid_required_fields"]]
    if train.empty or validation.empty:
        raise ValueError("A tuning fold has no observations.")
    return train.sort_values(["scheduled_time_local", "row_id"]), validation.sort_values(["scheduled_time_local", "row_id"])


def model_parameters(candidate, config, iterations=None):
    native = candidate["encoding"] == "native"
    if candidate["encoding"] not in ["one_hot", "native"]:
        raise ValueError("Unknown categorical encoding.")
    return dict(
        iterations=iterations or candidate["iterations"], depth=candidate["depth"],
        learning_rate=config["learning_rate"], l2_leaf_reg=config["l2_leaf_reg"],
        loss_function="MAE", eval_metric="MAE", random_seed=config["random_seed"],
        thread_count=config["thread_count"], allow_writing_files=False,
        cat_features=CATEGORICAL_FEATURES, one_hot_max_size=2 if native else 255,
        has_time=native, counter_calc_method="SkipTest",
    )


def rank_candidates(records, candidates):
    ranking = []
    for candidate in candidates:
        folds = [r for r in records if r["candidate"] == candidate["name"]]
        if not folds:
            raise ValueError("Missing candidate fold scores.")
        ranking.append({
            "candidate": candidate["name"],
            "mean_validation_mae_seconds": float(np.mean([r["validation"]["mae_seconds"] for r in folds])),
            "final_tree_count": int(np.median([r["tree_count"] for r in folds])),
            "fold_mae_seconds": [r["validation"]["mae_seconds"] for r in folds],
        })
    return sorted(ranking, key=lambda r: (r["mean_validation_mae_seconds"], r["final_tree_count"], r["candidate"]))


def run_cv(development, config, report_dir):
    records = []
    for fold_number, fold in enumerate(config["folds"], 1):
        training, validation = fold_frames(development, config, fold)
        x_train, x_val = build_features(training), build_features(validation)
        y_train = training["delay_seconds"].to_numpy(dtype=float)
        y_val = validation["delay_seconds"].to_numpy(dtype=float)
        for candidate in config["candidates"]:
            started = time.monotonic()
            print(f"Fold {fold_number}/{len(config['folds'])}: {candidate['name']}", flush=True)
            model = CatBoostRegressor(**model_parameters(candidate, config))
            model.fit(x_train, y_train, eval_set=(x_val, y_val), use_best_model=True,
                      early_stopping_rounds=config["early_stopping_rounds"], verbose=1000)
            pred = model.predict(x_val, thread_count=config["thread_count"])
            records.append({
                "fold": fold_number, "candidate": candidate["name"], "window": fold,
                "training_rows": len(training), "validation_rows": len(validation),
                "tree_count": model.tree_count_, "validation": metrics(y_val, pred),
                "elapsed_seconds": time.monotonic() - started,
            })
            write_json(report_dir / "cv_results.json", records)
            print(f"Completed: MAE={records[-1]['validation']['mae_seconds']:.2f}s; trees={model.tree_count_}", flush=True)
    return records


def save_model(training, candidate, trees, config, folder, data_hash):
    folder.mkdir(parents=True, exist_ok=True)
    x = build_features(training)
    model = CatBoostRegressor(**model_parameters(candidate, config, trees))
    model.fit(x, training["delay_seconds"].to_numpy(dtype=float), verbose=1000)
    model.save_model(str(folder / "catboost.cbm"))
    write_json(folder / "metadata.json", {
        "route": "BLUE", "timezone": "America/Winnipeg", "target": "delay_seconds", "positive_means": "late",
        "features": FEATURES, "categorical_features": CATEGORICAL_FEATURES,
        "training_vocabulary": {name: sorted(x[name].unique().tolist()) for name in CATEGORICAL_FEATURES},
        "candidate": candidate, "tree_count": trees, "config": config, "prepared_sha256": data_hash,
        "selection_rule": "Mean all-valid MAE across three validation weeks; median fold tree count for refit",
    })
    loaded = CatBoostRegressor()
    loaded.load_model(str(folder / "catboost.cbm"))
    sample = x.iloc[::max(1, len(x) // 2000)]
    np.testing.assert_allclose(model.predict(sample), loaded.predict(sample), atol=1e-10, rtol=0)
    return loaded


def report_and_plot(result, ranking, config, report_dir):
    labels = {"grouped_median": "Grouped median", "reference": "Reference CatBoost", "selected": "Selected recipe"}
    lines = ["# BLUE tuning experiment", "",
             f"Selected **{ranking[0]['candidate']}** using mean validation MAE across three expanding-training folds. "
             "The number of refit trees is the median of its fold-specific best tree counts.", "",
             "## Validation comparison", "",
             "| Recipe | Mean MAE | Fold 1 | Fold 2 | Fold 3 | Refit trees |", "|---|---:|---:|---:|---:|---:|"]
    for row in ranking:
        lines.append(f"| {row['candidate']} | {row['mean_validation_mae_seconds']:.2f} s | "
                     + " | ".join(f"{x:.2f}" for x in row["fold_mae_seconds"]) + f" | {row['final_tree_count']} |")
    lines += ["", "Validation windows are August 10–16, August 17–23, and August 24–30. Each model trains only on earlier eligible rows, starting July 25. "
              "All valid validation observations, including flagged records, contribute to selection. "
              "Native categorical candidates use chronological category processing and exclude validation rows from category counters. "
              "Validation MAE chooses the recipe; these values themselves are development scores.", "",
              "## Fresh holdout comparison", "",
              f"After selection, reference and selected recipes are refitted on the same **{result['training_rows']:,} eligible rows before September 6**. "
              f"Their tree counts come from validation only. The holdout is **September 10–12**, with **{result['holdout_rows']:,} observations**. "
              "The grouped-median comparator is also refitted on those training rows. Earlier experiments and models remain available.", "",
              "| Method | MAE, all valid | MAE, unflagged | Within ±2 minutes, all valid |", "|---|---:|---:|---:|"]
    for name, label in labels.items():
        all_rows, unflagged = result["scores"]["all_valid"][name], result["scores"]["unflagged"][name]
        lines.append(f"| {label} | {all_rows['mae_seconds']:.2f} s | {unflagged['mae_seconds']:.2f} s | {all_rows['within_120_seconds']:.1%} |")
    gain = result["scores"]["all_valid"]["reference"]["mae_seconds"] - result["scores"]["all_valid"]["selected"]["mae_seconds"]
    outcome = (f"The selected recipe reduced holdout MAE by **{gain:.2f} seconds**." if gain > 0 else
               f"The selected recipe did not improve the fresh holdout: MAE was **{-gain:.2f} seconds worse** than the reference. The default model remains unchanged.")
    lines += ["", outcome + " "
              "This is a three-day holdout, with Thursday, Friday, and Saturday observations only. It is not evidence of long-term or winter performance. "
              "Do not compare these errors directly with earlier reports, which used different test dates. "
              "Do not choose another recipe based on these holdout scores; reserve newly collected dates for further final evaluation.", "",
              "## Prediction use", "", "The selected model can be used with the existing predictor by adding `--model-dir models/tuned`. "
              "It uses the same prediction-time feature contract: stop, destination, day type, calendar weekday, and minute of day. "
              "It still does not observe the current bus's progress.", "",
              "## Files", "", "- `ranking.json` and `cv_results.json`: validation selection evidence.",
              "- `predictions.parquet`: holdout targets and predictions for all comparators.",
              "- `metrics.json`: complete holdout metrics, including errors by date.",
              "- `../../models/tuned/`: selected native model and metadata, compatible with `src.predict`.",
              "- `../../models/tuning_reference/`: reference recipe refit for the matched comparison.",
              "- `manifest.json`: configuration, data hash, and completed checks.", "",
              "[CatBoost categorical and time-order settings](https://catboost.ai/docs/en/references/training-parameters/common).", ""]
    (report_dir / "report.md").write_text("\n".join(lines))
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    fig.suptitle("BLUE tuning: validation selection and fresh holdout", fontsize=16)
    axes[0].barh([r["candidate"] for r in ranking], [r["mean_validation_mae_seconds"] for r in ranking], color="#2674AF")
    axes[0].invert_yaxis()
    axes[0].set_title("Mean MAE across three validation weeks")
    axes[0].set_xlabel("Seconds · lower is better")
    scores = result["scores"]["all_valid"]
    vals = [scores[name]["mae_seconds"] for name in labels]
    bars = axes[1].barh(list(labels.values()), vals, color=["#AAB4C3", "#2674AF", "#168477"])
    axes[1].invert_yaxis()
    axes[1].set_xlim(0, max(vals) * 1.15)
    for bar, val in zip(bars, vals):
        axes[1].text(val + 1, bar.get_y() + bar.get_height() / 2, f"{val:.1f}", va="center")
    axes[1].set_title("September 10–12 holdout MAE")
    axes[1].set_xlabel("Seconds · all valid observations")
    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(report_dir / "comparison.png", dpi=150)
    plt.close(fig)


def run(config_path):
    config_path = Path(config_path).resolve()
    root, config = config_path.parent, json.loads(config_path.read_text())
    validate_config(config)
    report_dir = root / "reports/tuning"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = root / config["prepared_data"]
    with path.open("rb") as stream:
        data_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    frame = pd.read_parquet(path)
    development = frame.loc[frame["scheduled_time_local"].lt(config["final_training_end_exclusive"])]
    write_json(report_dir / "experiment_plan.json", {"config": config, "prepared_sha256": data_hash})
    records = run_cv(development, config, report_dir)
    ranking = rank_candidates(records, config["candidates"])
    write_json(report_dir / "ranking.json", ranking)
    chosen = ranking[0]
    reference = next(r for r in ranking if r["candidate"] == "reference")
    candidates = {c["name"]: c for c in config["candidates"]}
    training = development.loc[development["baseline_eligible"] & development["scheduled_time_local"].ge(config["train_start"])].sort_values(["scheduled_time_local", "row_id"])
    print(f"Selected {chosen['candidate']}; refitting on {len(training):,} rows", flush=True)
    selected_model = save_model(training, candidates[chosen["candidate"]], chosen["final_tree_count"], config, root / "models/tuned", data_hash)
    reference_model = save_model(training, candidates["reference"], reference["final_tree_count"], config, root / "models/tuning_reference", data_hash)
    baseline = fit_baseline(training, config["minimum_group_observations"])
    # Holdout accessed after recipe, iterations, and final fits are fixed.
    test = frame.loc[frame["scheduled_time_local"].ge(config["holdout_start"]) & frame["scheduled_time_local"].lt(config["holdout_end_exclusive"])
                     & frame["valid_required_fields"]].copy()
    if test.empty or set(test["row_id"]) & set(training["row_id"]):
        raise ValueError("Empty or overlapping holdout.")
    x_test = build_features(test)
    predictions = {"grouped_median": predict_baseline(baseline, test)[0], "reference": reference_model.predict(x_test), "selected": selected_model.predict(x_test)}
    result = {"selected_recipe": chosen["candidate"], "training_rows": len(training), "holdout_rows": len(test), "scores": score_predictions(test, predictions)}
    stored = test[["row_id", "scheduled_time_local", "delay_seconds", "baseline_eligible"]].copy()
    for name, values in predictions.items():
        stored[f"prediction_{name}"] = values
    result["by_date"] = {str(date): {name: metrics(part["delay_seconds"], part[f"prediction_{name}"]) for name in predictions}
                         for date, part in stored.groupby(stored["scheduled_time_local"].dt.date)}
    stored.to_parquet(report_dir / "predictions.parquet", index=False, compression="zstd")
    loaded = pd.read_parquet(report_dir / "predictions.parquet")
    pd.testing.assert_frame_equal(stored.reset_index(drop=True), loaded)
    for name in predictions:
        np.testing.assert_allclose(float((loaded[f"prediction_{name}"] - loaded["delay_seconds"]).abs().mean()), result["scores"]["all_valid"][name]["mae_seconds"], atol=1e-10)
    write_json(report_dir / "metrics.json", result)
    report_and_plot(result, ranking, config, report_dir)
    write_json(report_dir / "manifest.json", {"completed_at_utc": datetime.now(timezone.utc).isoformat(), "config": config,
               "prepared_sha256": data_hash, "checks": ["CV windows precede holdout", "Selection uses validation only",
               "Refit counts fixed before holdout", "Training and holdout Row IDs disjoint", "Saved models reproduce predictions",
               "Stored holdout predictions reconcile with metrics"]})
    print(json.dumps(result["scores"], indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="tuning_config.json")
    run(parser.parse_args().config)
