# Do revisions and earlier-stop estimates add information?

**Exploratory test of fixed prediction rules on the previously inspected September 14 twenty-minute capture.** No model is fitted and no rule is selected for deployment. These are hypotheses tested on a very small reused sample.

The scored sample has **9 distinct departures across 6 trips**. A departure can occur at several horizons and in several tables; these are not independent observations.

## Inputs and rules

- Revision: change in the same event’s estimate over approximately three minutes. Extrapolate that rate for at most three minutes, capped at a ±120-second adjustment.
- Earlier stop: estimated delay at the closest earlier stop in the same trip response, requiring the same bus ID. Its estimated passage must precede snapshot receipt by no more than ten minutes.
- Blend: average the target API estimate and earlier-stop delay equally.
- Combined: average the trend-adjusted API estimate and earlier-stop delay equally.

**Earlier-stop estimates are a proxy, not measured bus progress.** The capture has no actual stop-passage telemetry or vehicle positions. A past estimated passage does not confirm that the bus passed the stop. Estimated clock times are conservatively resolved using a dated target event known at the cutoff.

All snapshot receipt times precede the prediction cutoff. Current/history stop estimates must be fresh within 90 seconds of their respective cutoffs; trip snapshots must be no more than four minutes old. Missing or changed bus IDs, ambiguous dates, inconsistent schedules and stale data are excluded. Later measured outcomes are loaded only after feature extraction.

## Paired comparisons

MAE is in seconds; lower is better. Each row compares methods on exactly the same departures. Availability differs between rows and horizons. The larger upstream table is not directly comparable with the revision table.

### Revision available

| Minutes ahead | Departures | Trips | api MAE | trend MAE |
|---|---:|---:|---:|---:|
| 1 | 4 | 3 | 38.5 | 35.0 |
| 2 | 4 | 3 | 54.5 | 53.0 |
| 5 | 4 | 3 | 74.8 | 72.5 |
| 10 | 4 | 4 | 63.2 | 63.2 |
| 20 | 4 | 3 | 28.2 | 28.2 |

### Upstream available

| Minutes ahead | Departures | Trips | api MAE | upstream MAE | blend MAE |
|---|---:|---:|---:|---:|---:|
| 1 | 5 | 3 | 39.6 | 46.0 | 42.8 |
| 2 | 4 | 3 | 54.5 | 47.5 | 44.0 |
| 5 | 4 | 3 | 74.8 | 67.0 | 70.9 |
| 10 | 5 | 4 | 57.8 | 67.2 | 55.9 |
| 20 | 4 | 3 | 28.2 | 103.8 | 65.8 |

### Both available

| Minutes ahead | Departures | Trips | api MAE | trend MAE | upstream MAE | blend MAE | combined MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 3 | 38.5 | 35.0 | 42.5 | 40.5 | 38.7 |
| 2 | 4 | 3 | 54.5 | 53.0 | 47.5 | 44.0 | 43.2 |
| 5 | 4 | 3 | 74.8 | 72.5 | 67.0 | 70.9 | 69.8 |
| 10 | 4 | 4 | 63.2 | 63.2 | 75.8 | 69.5 | 69.5 |
| 20 | 4 | 3 | 28.2 | 28.2 | 103.8 | 65.8 | 65.8 |

## What this run shows

The combined rule has lower MAE at **2, 5 minutes** and higher MAE at **1, 10, 20 minutes** on the common-availability cohorts. The effect is inconsistent across horizons. Small near-departure gains warrant a larger future test, but do not justify replacing the API estimate. The separate tables above show whether any gain comes from estimate revisions, the earlier-stop proxy, or their combination.

## Interpretation limits

An improvement here would support collecting more data to test the signal; it would not validate a deployable model. A worse fixed rule would not prove the underlying feature is useless to a learned model. Rules may double-count information already incorporated into the API estimate, and a trend may not persist. There is insufficient independent data for a credible held-out learned-feature ablation. Reserve newly collected dates for that evaluation.

Only dated target stop events were scored. Their measured outcomes were conservatively linked by route, stop, destination and exact schedule time; unmatched outcomes remain missing. Multiple stops can belong to the same trip. No measured upstream outcomes are used as input.

Protocol, missing-feature counts, all extracted features and row-level scored predictions are saved alongside this report.

```bash
.venv/bin/python -m src.test_live_signals --run-directory reports/collector_20min_20260915T002352Z \
  --outcomes reports/live_evaluation_20260914/outcomes.json --output-directory reports/live_signals_20260914
```
