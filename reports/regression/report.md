# BLUE regression experiment

On 60,592 observed stop departures during August 31–September 5, CatBoost reduced mean absolute error by **1.7 seconds (1.4%)** relative to a grouped-median baseline fit on exactly the same training rows.

This is a comparison on a **new six-day test period**. Its scores are not directly comparable with the first report's August 24–30 scores.

## Experiment design

| Split | Local dates | Observations |
|---|---|---:|
| Training, eligible only | July 25–August 23 | 248,650 |
| Validation, all valid | August 24–30 | 64,669 |
| Test, all valid | August 31–September 5 | 60,592 |

The first experiment's test week is now explicitly used as validation. The new test starts after all development dates. September 6–9 have no BLUE observations, so the test ends before that gap; it includes Monday through Saturday and no Sunday. September 10–12 are not scored. Nonzero daily counts do not prove collection completeness.

Both models fit the same eligible training observations. Primary validation and test scores include flagged extremes and duplicate candidates. Secondary scores exclude those flagged rows but use the same fitted models and predictions. No missing departures are generated or treated as cancellations.

## What the regression model learns

CatBoost is a model made from many decision trees. Unlike a fixed median lookup, it can learn combinations of stop, destination, day type, calendar weekday, and scheduled minute of day, and share patterns across similar observations.

Inputs are exactly: `stop_number`, `destination`, `day_type`, `day_of_week`, and `minute_of_day`. Weekday and time-of-day features are derived from the scheduled local timestamp. Categorical values are passed as strings and one-hot encoded for this dataset. Row IDs, observed deviation, quality flags, split labels, and future/current bus delays are not features. The route is fixed to BLUE. No month effect is fitted from this short summer sample.

Two predeclared candidates (depths [4, 6]) use MAE loss, learning rate 0.05, at most 1200 trees, and seed 42. Early stopping and candidate selection use validation MAE only. The selected model has depth **6** and **1197 trees**. It is saved before the new test is scored; there is no refit on validation or test rows.

| Candidate | Retained trees | Validation MAE (seconds) |
|---|---:|---:|
| Depth 4 | 1200 | 118.55 |
| Depth 6 | 1197 | 117.21 |

## New test results

| Method | MAE, all valid | MAE, unflagged | Within ±2 min, all valid |
|---|---:|---:|---:|
| Always on time | 144.18 s | 129.84 s | 63.7% |
| Global median | 135.50 s | 121.07 s | 64.9% |
| Grouped median | 123.24 s | 108.79 s | 68.7% |
| CatBoost regression | 121.54 s | 107.09 s | 69.1% |

Lower MAE means a smaller average prediction error. Within ±2 minutes is a prediction-error measure, not the percentage of buses running on time.

## Differences by destination

| Destination | Test observations | Grouped MAE | Regression MAE |
|---|---:|---:|---:|
| Downtown | 2,084 | 62.46 s | 62.81 s |
| St. Norbert | 18,924 | 140.72 s | 136.58 s |
| Unicity Shopping Centre | 26,952 | 121.07 s | 120.60 s |
| University of Manitoba | 12,632 | 111.70 s | 110.70 s |

The metrics file also gives per-date results, tail errors, signed bias, and how many test categories were unseen in training. Rows can share a bus trip and are not independent samples. Six test dates are too few to establish robust seasonal generalization; no statistical-significance claim is made.

## Interpretation and next step

This predicts historical-pattern **departure delay**, not an arrival ETA or the evolving delay of a live bus. The CSV has no vehicle/trip IDs or stop sequence. Do not manufacture previous-stop delay with a row shift. After reviewing this result, use validation for further model choices and collect or reserve a new future period for another final test.

The saved predictor can now be called with a stop, destination, day type, and scheduled time. A practical next improvement is collecting trip-linked live observations so the model can use the particular bus's recent delay.

## Outputs and reproducibility

- `../../models/regression/catboost.cbm`: selected model in CatBoost's native format.
- `../../models/regression/metadata.json`: exact feature contract, training category vocabulary, selection details, and data hash.
- `../../models/regression/grouped_median.json`: comparator fitted on the new training period.
- `metrics.json`: full model comparisons, including per-date and per-destination scores.
- `predictions.parquet`: row-level validation and test predictions for every comparison method.
- `comparison.png`: aggregate, daily, and validation-learning plots.
- `manifest.json`: configuration, software versions, artifact hashes, and checks.

Model selection code receives only training and validation frames. Predictions from the saved model were checked against the in-memory model, and stored test predictions were independently reconciled with reported MAE.

## References

[CatBoost regressor](https://catboost.ai/docs/en/concepts/python-reference_catboostregressor), [validation and parameter tuning](https://catboost.ai/docs/en/concepts/parameter-tuning), [MAE objective](https://catboost.ai/docs/en/concepts/loss-functions-regression). Data provenance and parsing decisions are documented in `../first_baseline.md`.
