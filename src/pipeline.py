"""Run with python -m src.pipeline --config config.json."""

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform

import numpy as np
import pandas as pd

from .baseline import evaluate
from .data import ingest
from .plots import plot_overview


def write_json(path, value):
    def native(item):
        if isinstance(item, np.generic):
            return item.item()
        if isinstance(item, pd.Timestamp):
            return item.isoformat()
        raise TypeError(f"Cannot encode {type(item)}")
    path.write_text(json.dumps(value, indent=2, default=native, allow_nan=False) + "\n")


def route_profile(frame, source_profile):
    calendar = pd.date_range(pd.Timestamp(source_profile["date_min"]).normalize(),
                             pd.Timestamp(source_profile["date_max"]).normalize())
    daily = frame.groupby("scheduled_date").size().reindex(calendar, fill_value=0)
    split_counts = {}
    for name, group in frame.groupby("split", observed=True):
        split_counts[str(name)] = {
            "rows": len(group), "baseline_eligible": int(group["baseline_eligible"].sum()),
            "extreme_delay": int(group["flag_extreme_delay"].sum()),
            "duplicate_candidates": int(group["flag_duplicate_candidate"].sum()),
            "date_min": str(group["scheduled_time_local"].min()),
            "date_max": str(group["scheduled_time_local"].max()),
        }
    return {
        "rows": len(frame), "unique_stops": frame["stop_number"].nunique(),
        "destinations": frame["destination"].value_counts().to_dict(),
        "baseline_eligible_rows": int(frame["baseline_eligible"].sum()),
        "invalid_required_rows": int((~frame["valid_required_fields"]).sum()),
        "flags": {c: int(frame[c].sum()) for c in frame.columns if c.startswith("flag_")},
        "extreme_early_rows": int((frame["flag_extreme_delay"] & frame["delay_seconds"].lt(0)).sum()),
        "extreme_late_rows": int((frame["flag_extreme_delay"] & frame["delay_seconds"].gt(0)).sum()),
        "extreme_examples": frame.loc[frame["flag_extreme_delay"]].assign(
            magnitude=lambda f: f["delay_seconds"].abs()
        ).nlargest(6, "magnitude")[["row_id", "stop_number", "destination", "scheduled_time_local", "delay_seconds"]].to_dict(orient="records"),
        "daily_rows": {date.strftime("%Y-%m-%d"): int(n) for date, n in daily.items()},
        "missing_calendar_dates": [date.strftime("%Y-%m-%d") for date in daily.index[daily.eq(0)]],
        "split_counts": split_counts,
        "delay_quantiles_seconds": {str(q): float(frame["delay_seconds"].quantile(q)) for q in [0, .01, .5, .95, .99, 1]},
        "extreme_by_date": {
            date.strftime("%Y-%m-%d"): int(n)
            for date, n in frame.loc[frame["flag_extreme_delay"]].groupby("scheduled_date").size().items()
        },
    }


def make_report(root, config, source, route, result):
    c = config
    lines = [
        f"# {c['route']} data preparation and departure-delay baseline", "",
        f"Prepared from `{source['source_file']}`. One record represents a scheduled stop departure, not a complete bus trip.", "",
        "## Data coverage", "",
        f"The source contains **{source['source_rows']:,} records** covering {source['date_min']} through {source['date_max']}. "
        f"The prepared {c['route']} subset retains **{route['rows']:,} records**, {route['unique_stops']} stops, and {len(route['destinations'])} destinations.", "",
        "Missing source dates: " + ", ".join(source["missing_calendar_dates"]) + ".",
        f"Missing {c['route']} dates: " + ", ".join(route["missing_calendar_dates"]) + ".",
        "See daily counts in `data_quality.json` and the coverage plot. Missing observations are not evidence of cancelled buses. "
        "Partial collection can affect other dates too; nonzero counts do not establish complete coverage.", "",
        "## Parsing and quality decisions", "",
        f"- Parsed {source['deviations_with_thousands_commas']:,} source deviations containing thousands commas. "
        f"After parsing, {source['invalid_deviations_after_comma_parsing']:,} source deviations and {source['invalid_scheduled_times']:,} scheduled times are invalid.",
        "- Normalized `delay_seconds = -deviation_seconds`: positive means late, negative means early. "
        "Original deviation, scheduled-time text, location, Row ID, and source record number are retained.",
        f"- Scheduled timestamps preserve local wall-clock time, interpreted as {c['local_timezone']}. "
        "The CSV supplies no offset. `scheduled_date` is the calendar date, not an inferred transit service date; `day_type` is retained separately.",
        f"- Flagged {route['flags']['flag_extreme_delay']:,} {c['route']} records with absolute delay greater than "
        f"{c['extreme_delay_seconds']:,} seconds. This is an exploratory review threshold, not an official validity rule. "
        "There is no evidence sufficient to correct these values, so all remain in the prepared file.",
        f"- Of those extreme records, {route['extreme_early_rows']:,} indicate early departures and "
        f"{route['extreme_late_rows']:,} indicate late departures. The most extreme early value is "
        f"{abs(route['delay_quantiles_seconds']['0']) / 3600:.2f} hours before schedule. "
        "Example Row IDs, stops, destinations, and timestamps are retained in `data_quality.json` for inspection. "
        "These flags identify suspicious measurements; they do not establish a specific GPS, clock, or scheduling fault.",
        f"- Flagged {route['flags']['flag_duplicate_candidate']:,} additional records with identical source fields except Row ID. "
        "These are duplicate candidates, not confirmed errors: the CSV has no trip identifier. "
        "`duplicate_of_row_id` identifies the first matching source record, and all copies remain available.",
        f"- `baseline_eligible` selects {route['baseline_eligible_rows']:,} records with valid required fields, destination, and day type, "
        "without extreme-delay or duplicate-candidate flags. Invalid locations are flagged separately because the baseline does not use coordinates.",
        "- The baseline fits eligible training rows. Primary evaluation includes **all parseable validation/test observations**, "
        "including extreme delays and duplicate candidates. A second evaluation shows the unflagged subset with the same frozen predictions.", "",
        "## Chronological experiment", "",
        "| Split | Local interval, end excluded | Retained rows | Eligible rows |",
        "|---|---|---:|---:|",
    ]
    for label, start, end in [
        ("train", c["train_start"], c["validation_start"]),
        ("validation", c["validation_start"], c["test_start"]),
        ("test", c["test_start"], c["test_end_exclusive"]),
    ]:
        count = route["split_counts"][label]
        lines.append(f"| {label} | {start} to {end} | {count['rows']:,} | {count['baseline_eligible']:,} |")
    lines += ["", "Dates after the test interval are reserved for a subsequent check and have no baseline scores. "
              "Delay-by-hour, delay-by-stop, and distribution plots use eligible training rows only.", "",
              f"The predictor takes the training median for stop, destination, day type, and hour, with at least "
              f"{c['minimum_group_observations']} observations per group. Sparse or unseen groups fall back through "
              "stop/destination/day type, stop/destination, destination/day type/hour, destination/day type, destination, "
              f"and finally the global training median ({result['training_global_median_seconds']:.1f} seconds). "
              "Destination is a useful grouping field, not a unique trip or route-variant identifier. "
              "No evaluation labels enter these lookups, and no settings were selected using test performance.", "",
              "## Baseline results", "",
              "Errors are in seconds. Lower MAE is better; higher within-two-minutes coverage is better.", "",
              "| Split / population | Observations | On-time MAE | Global-median MAE | Grouped-median MAE | Grouped within ±2 min |",
              "|---|---:|---:|---:|---:|---:|"]
    for split in ["validation", "test"]:
        for population in ["all_valid", "unflagged"]:
            scores = result["scores"][split][population]
            g = scores["grouped_median"]
            lines.append(f"| {split} / {population.replace('_', ' ')} | {g['n']:,} | "
                         f"{scores['scheduled_on_time']['mae_seconds']:.1f} | "
                         f"{scores['training_global_median']['mae_seconds']:.1f} | "
                         f"{g['mae_seconds']:.1f} | {g['within_120_seconds']:.1%} |")
    lines += ["", "`baseline_metrics.json` also includes RMSE, median and 90th-percentile absolute error, "
              "signed bias, fallback usage, and test errors by destination and date. "
              "`baseline_predictions.parquet` contains the observation-level validation/test predictions.", "",
              "## What this result means", "",
              "This is a historical-pattern departure-delay baseline for observed stop events during this summer snapshot. "
              "It does not measure winter performance, identify cancellations, predict arrivals, or use a bus's current delay. "
              "Multiple observations can belong to one bus trip, so the row count is not a count of independent trips.", "",
              "The next experiment is a small supervised regression model using the same available features and a fresh later "
              "evaluation period after checking its coverage. Compare against these frozen median lookups. "
              "A previous-stop or current-delay model requires reliable trip-linked observations not present in this CSV.", "",
              "## Files", "",
              "- `../data/processed/blue_clean.parquet`: every BLUE row, typed fields, quality flags, and split labels.",
              "- `blue_review.parquet`: rows needing review; these are also retained in the clean file.",
              "- `blue_overview.png`: data coverage and training-period patterns.",
              "- `data_quality.json`: source hash, coverage, parsing checks, and row reconciliation.",
              "- `../models/historical_median.json`: training-only lookup tables and fallback rules.", "",
              "## Source", "", f"[City of Winnipeg dataset]({source['source_url']}). "
              "The source documentation warns that transmission and GPS issues can produce missing or erroneous records. "
              "A documentation snapshot is saved in `../data/source_metadata.json`.", "",
              f"Source SHA-256: `{source['source_sha256']}`.", ""]
    (root / "reports" / "first_baseline.md").write_text("\n".join(lines))


def run(config_path):
    config_path = Path(config_path).resolve()
    root = config_path.parent
    config = json.loads(config_path.read_text())
    if config["route"] != "BLUE":
        raise ValueError("This first experiment is configured for BLUE; adapt output/report names before changing route.")
    if config["local_timezone"] != "America/Winnipeg":
        raise ValueError("This source uses Winnipeg local scheduled times.")
    if config["extreme_delay_seconds"] <= 0 or config["minimum_group_observations"] < 1 or config["chunksize"] < 1:
        raise ValueError("Thresholds and chunksize must be positive.")
    for folder in ["data/processed", "reports", "models"]:
        (root / folder).mkdir(parents=True, exist_ok=True)
    source = root / config["input_csv"]
    frame, source_profile = ingest(source, config)
    profile = route_profile(frame, source_profile)
    result, predictions, model = evaluate(frame, config["minimum_group_observations"])
    data_path = root / "data/processed/blue_clean.parquet"
    frame.to_parquet(data_path, index=False, compression="zstd")
    review_mask = ~frame["baseline_eligible"] | frame["flag_invalid_location"]
    frame.loc[review_mask].to_parquet(root / "reports/blue_review.parquet", index=False, compression="zstd")
    predictions.to_parquet(root / "reports/baseline_predictions.parquet", index=False, compression="zstd")
    quality = {"source": source_profile, "route": profile, "config": config}
    write_json(root / "reports/data_quality.json", quality)
    write_json(root / "reports/baseline_metrics.json", result)
    write_json(root / "models/historical_median.json", {
        "route": config["route"], "target": "delay_seconds", "positive_target_means": "late",
        "training_start": config["train_start"], "training_end_exclusive": config["validation_start"],
        "minimum_group_observations": config["minimum_group_observations"],
        "global_median": model["global_median"],
        "lookups": [{"keys": keys, "groups": table.to_dict(orient="records")} for keys, table in model["lookups"]],
    })
    plot_overview(frame, config, root / "reports/blue_overview.png")
    make_report(root, config, source_profile, profile, result)
    # Verify the persisted dataset, not just the in-memory calculations.
    stored = pd.read_parquet(data_path)
    pd.testing.assert_frame_equal(stored, frame)
    assert len(stored) == source_profile["route_counts"][config["route"]]
    assert stored.loc[stored["valid_required_fields"], "scheduled_time_local"].is_monotonic_increasing
    assert int(stored["baseline_eligible"].sum()) == profile["baseline_eligible_rows"]
    assert np.isfinite(predictions["prediction_seconds"]).all()
    write_json(root / "reports/run_manifest.json", {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "dependencies": {p: importlib.metadata.version(p) for p in ["pandas", "numpy", "pyarrow", "matplotlib"]},
        "source_sha256": source_profile["source_sha256"], "config": config,
        "checks": ["Parquet round-trip preserves all columns, types, and values", "Source route count reconciles",
                   "Valid observations sorted by local scheduled time", "Training precedes evaluation",
                   "Every validation/test prediction is finite"],
    })
    print(f"Prepared and verified {len(frame):,} BLUE rows; report: {root / 'reports/first_baseline.md'}", flush=True)
    print(json.dumps(result["scores"]["test"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    run(parser.parse_args().config)
