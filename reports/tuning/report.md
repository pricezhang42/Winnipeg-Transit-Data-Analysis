# BLUE tuning experiment

Selected **longer_one_hot** using mean validation MAE across three expanding-training folds. The number of refit trees is the median of its fold-specific best tree counts.

## Validation comparison

| Recipe | Mean MAE | Fold 1 | Fold 2 | Fold 3 | Refit trees |
|---|---:|---:|---:|---:|---:|
| longer_one_hot | 122.22 s | 128.34 | 123.06 | 115.27 | 3998 |
| native_depth8 | 122.67 s | 128.57 | 124.19 | 115.25 | 1698 |
| native_depth6 | 122.92 s | 128.64 | 124.47 | 115.65 | 3397 |
| reference | 123.94 s | 130.05 | 124.55 | 117.21 | 1197 |

Validation windows are August 10–16, August 17–23, and August 24–30. Each model trains only on earlier eligible rows, starting July 25. All valid validation observations, including flagged records, contribute to selection. Native categorical candidates use chronological category processing and exclude validation rows from category counters. Validation MAE chooses the recipe; these values themselves are development scores.

## Fresh holdout comparison

After selection, reference and selected recipes are refitted on the same **373,709 eligible rows before September 6**. Their tree counts come from validation only. The holdout is **September 10–12**, with **29,138 observations**. The grouped-median comparator is also refitted on those training rows. Earlier experiments and models remain available.

| Method | MAE, all valid | MAE, unflagged | Within ±2 minutes, all valid |
|---|---:|---:|---:|
| Grouped median | 204.39 s | 192.83 s | 52.3% |
| Reference CatBoost | 202.14 s | 190.56 s | 52.9% |
| Selected recipe | 202.77 s | 191.16 s | 52.5% |

The selected recipe did not improve the fresh holdout: MAE was **0.63 seconds worse** than the reference. The default model remains unchanged. This is a three-day holdout, with Thursday, Friday, and Saturday observations only. It is not evidence of long-term or winter performance. Do not compare these errors directly with earlier reports, which used different test dates. Do not choose another recipe based on these holdout scores; reserve newly collected dates for further final evaluation.

## Prediction use

The selected model can be used with the existing predictor by adding `--model-dir models/tuned`. It uses the same prediction-time feature contract: stop, destination, day type, calendar weekday, and minute of day. It still does not observe the current bus's progress.

## Files

- `ranking.json` and `cv_results.json`: validation selection evidence.
- `predictions.parquet`: holdout targets and predictions for all comparators.
- `metrics.json`: complete holdout metrics, including errors by date.
- `../../models/tuned/`: selected native model and metadata, compatible with `src.predict`.
- `../../models/tuning_reference/`: reference recipe refit for the matched comparison.
- `manifest.json`: configuration, data hash, and completed checks.

[CatBoost categorical and time-order settings](https://catboost.ai/docs/en/references/training-parameters/common).
