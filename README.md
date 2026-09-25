# Winnipeg Transit departure-delay prediction

Prepare the downloaded on-time performance CSV, audit the data, and establish a historical-median baseline for BLUE. The original download remains the source of record. No API key or live service is required.

## Run

Python 3.12 is the verified version. To set up a fresh environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m src.pipeline --config config.json
```

The workspace already has a working `.venv`. Run the last two commands to verify and reproduce the experiment. The CSV path, thresholds, and date boundaries are in `config.json`. Output files are regenerated on each run.

Read [the first results](reports/first_baseline.md) and [view the plots](reports/blue_overview.png).

## Regression model

The next experiment compares a CatBoost regressor with medians fitted on the same training rows. Its configuration and outputs are separate from the first baseline experiment:

```bash
.venv/bin/python -m src.regression --config regression_config.json
```

This trains on July 25–August 23, selects between tree depths 4 and 6 on August 24–30, and evaluates the selected model on August 31–September 5. The first experiment's test week is now explicitly validation; the final comparison uses a later six-day period with no Sunday. The data has no BLUE records for September 6–9. September 10–12 remain outside this experiment.

The regression inputs are stop, destination, day type, calendar weekday, and minute of day. All are available before the departure. Actual delays, Row IDs, and quality flags never become input features. Model selection uses validation MAE only. Both the regressor and the median comparator fit eligible training observations, and both are evaluated on all valid observations plus the unflagged subset. Comparing different reports' test scores directly would be misleading because the dates differ.

The fitted model is saved as `models/regression/catboost.cbm`. Read [the regression report](reports/regression/report.md) and [comparison plots](reports/regression/comparison.png). Re-running the regression command overwrites that experiment's outputs; it does not modify the prepared data or the first baseline report.

To make an illustrative prediction (this does not query or confirm an actual scheduled bus):

```bash
.venv/bin/python -m src.predict \
  --stop 11027 \
  --destination "St. Norbert" \
  --day-type Weekday \
  --scheduled-time "2026-09-15T08:30:00"
```

The time is interpreted in `America/Winnipeg`; an explicit UTC offset is converted to Winnipeg time. Supply the actual service day type, particularly for holidays. Positive predicted delay means late. The output includes the predicted local departure time and any categories absent from training. Such unseen cases have less direct support in the training data.

For a Python call:

```python
from src.predict import predict_departure

result = predict_departure(
    "models/regression",
    stop_number="11027",
    destination="St. Norbert",
    day_type="Weekday",
    scheduled_time="2026-09-15T08:30:00",
)
```

This remains a historical-pattern departure predictor, not a live arrival ETA. Use a new future test period for further development after inspecting these results.

## What is prepared

`data/processed/blue_clean.parquet` contains every selected BLUE observation, sorted by scheduled time. Row/stop/route identifiers remain text. `deviation_raw`, `scheduled_time_raw`, `location_wkt`, and `source_record_number` preserve traceability to the CSV. The source record number is one-based and excludes the header; it is not necessarily a physical line number in a CSV with multiline fields.

The City records negative deviation for late departures. Parsing removes properly grouped thousands commas and computes `delay_seconds = -deviation_seconds`. A positive target therefore means late. Malformed numbers are flagged rather than converted to zero.

`scheduled_time_local` is a timezone-naive timestamp interpreted as Winnipeg local wall-clock time. The CSV contains no offset. `scheduled_date` is a calendar date, not an inferred transit service date. Features include stop, destination, the source's day type, calendar weekday (Monday=0), hour, minute, month, and parsed longitude/latitude. Month and minute are retained for future experiments but are not used by this median baseline.

Quality flags preserve extreme values, duplicate candidates, missing grouping fields, and invalid coordinates. An absolute delay above one hour is a review flag, not proof of an error. All observations remain in the prepared file, and flagged observations also appear in `reports/blue_review.parquet`.

`baseline_eligible` excludes records with invalid required fields, missing destination/day type, delay magnitude over one hour, or an additional duplicate candidate. Missing coordinates alone do not prevent baseline use. No values are winsorized or imputed, and no missing departures are invented.

## Experiment

- Train: July 25–August 16, 2026.
- Validate: August 17–23.
- Test: August 24–30.
- Later dates remain reserved for another evaluation after coverage checks.

The model fits eligible training observations only. It predicts median delay for stop/destination/day type/hour, requiring at least 20 training observations per group. Sparse and unseen groups use successively broader training-only medians; the global training median is the final fallback. Model tables are saved in `models/historical_median.json`.

Scores compare a zero-delay prediction, the training global median, and grouped medians. Each is evaluated on both all parseable observations and the unflagged subset. This exposes the impact of anomalies without erasing them. The test is not used to tune thresholds or grouping rules. Before changing the model after seeing these results, choose a fresh later test period and use validation data for model selection.

These are stop-departure observations, not independent bus trips. There are no trip IDs, vehicle IDs, or stop sequences. Do not derive previous-stop delay with a row shift, claim arrival-time prediction, or interpret missing rows as cancellations. Destination does not uniquely identify a route variant.

## Files

- `src/data.py`: source audit, parsing, features, quality flags, and date splits.
- `src/baseline.py`: training-only lookup baseline, fallbacks, and evaluation.
- `src/plots.py`: training-period patterns and full-snapshot coverage.
- `src/pipeline.py`: writes and verifies the Parquet, report, plots, metrics, and manifest.
- `src/features.py`: shared training/inference feature contract.
- `src/regression.py`: bounded model selection, later evaluation, and saved model verification.
- `src/predict.py`: local command and Python interface to the trained regression model.
- `tests/test_pipeline.py`: parsing, record preservation, duplicate handling, boundaries, fallbacks, leakage, and metric checks.
- `tests/test_regression.py`: feature isolation, timezone handling, new split boundaries, and saved-model inference.
- `data/source_metadata.json`: City dataset documentation snapshot.

The raw CSV and generated Parquet files are excluded by `.gitignore`; source code, configuration, documentation, and compact reports can be versioned.

## Small tuning experiment

```bash
.venv/bin/python -m src.tune --config tuning_config.json
```

Four recipes were compared on three chronological validation weeks. Longer training improved validation MAE from 123.94 to 122.22 seconds, but did not improve the fresh September 10–12 holdout: the matched reference scored 202.14 seconds and the selected recipe 202.77 seconds. The original default model remains unchanged. The later period is now used for evaluation and is no longer an untouched test set. See [the tuning report](reports/tuning/report.md) for selection evidence and limitations.

## Live-data collector

The collector is implemented separately from the historical predictor and has been verified with live API requests. Save `WINNIPEG_TRANSIT_API_KEY` in `.env`, then run:

```bash
.venv/bin/python -m src.collect --continuous
```

This appends timestamped BLUE stop and trip snapshots to `data/live/transit.sqlite` until Ctrl+C. Use `--cycles 1` for a finite capture or `--status` for stored counts. The key is ignored by version-control patterns. No persistent collector service is installed automatically.

Read [collection instructions and data limitations](docs/live_collection.md). Trip responses currently provide clock-only times, which are preserved and flagged until dates can be resolved. API predictions are estimates, not actual departure labels; collecting them does not itself improve the fitted model.

## Initial evaluation of captured live estimates

September 14 measured departures became available after the twenty-minute capture. The evaluator matches dated stop snapshots to unique City outcomes, then uses the last fresh prediction available at each 5-, 10-, or 20-minute cutoff. It evaluates the Transit API estimates, not a newly trained model. [Initial live evaluation](reports/live_evaluation_20260914/report.md) includes the small-sample results, match coverage, source query, row-level predictions, and limitations.

```bash
.venv/bin/python -m src.evaluate_live \
  --run-directory reports/collector_20min_20260915T002352Z \
  --outcomes reports/live_evaluation_20260914/outcomes.json \
  --output-directory reports/live_evaluation_20260914
```

## Do live estimate changes help?

The saved capture now has an exploratory fixed-rule comparison of recent estimate revisions and earlier-stop estimates for the same trip and bus. All features use snapshots available by the forecast cutoff. Trip clock times are anchored to a dated target event already known at that cutoff; uncertain dates are rejected. Earlier-stop estimates remain proxies, not measured bus progress.

[Signal test results](reports/live_signals_20260914/report.md) show mixed effects on nine distinct departures across six trips. The combined rule helps at some near-departure horizons and hurts at others. No model was fitted or promoted. A learned-feature comparison needs additional independent dates.

```bash
.venv/bin/python -m src.test_live_signals \
  --run-directory reports/collector_20min_20260915T002352Z \
  --outcomes reports/live_evaluation_20260914/outcomes.json \
  --output-directory reports/live_signals_20260914
```

## Standalone visualization website

The dashboard runs in an ordinary browser with no Codex tools, ChatGPT account, or build step. From the repository root:

```bash
python3 -m http.server 8787 --bind 127.0.0.1 --directory dashboard/dist
```

Open [the dashboard](http://localhost:8787/) or [pass-up visualizations](http://localhost:8787/passups.html). The three views cover captured BLUE trips, historical BLUE departures, and reported pass-ups across all routes. Existing snapshots are bundled; viewing them does not require the collector or analysis environment.

The pass-up view includes monthly, route, hourly, weekday, and type charts with date/route/type filters, ID/destination search, and paginated records. Its September 25 CSV export contains 199,249 reports from December 2009 through September 24, 2026. Counts describe reported events, not passenger counts or rates. Regenerate it with Python's standard library:

```bash
python3 -m src.export_passups --source Transit_Pass-ups_20260925.csv
```

To host independently, serve the contents of `dashboard/dist` with any static HTTP host. See [dashboard instructions](dashboard/README.md) for hosting, refreshing each dataset, and validation. The original historical and trip snapshots remain separate from the newly downloaded CSVs.
