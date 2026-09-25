# Initial live prediction evaluation — September 14, 2026

This evaluates **the Winnipeg Transit API estimates captured during the twenty-minute test**, against later measured City departure records. No live-feature model has been trained.

Matched **13 of 21** distinct dated departure events. The capture contains 267 dated stop snapshots; repeated snapshots are not treated as independent departures.

| Minutes before scheduled departure | Departures | Trips | API MAE | On-time MAE | API within ±2 min |
|---|---:|---:|---:|---:|---:|
| 5 | 4 | 3 | 74.8 s | 77.0 s | 75.0% |
| 10 | 6 | 5 | 52.8 s | 52.8 s | 83.3% |
| 20 | 4 | 3 | 28.2 s | 28.2 s | 100.0% |

For each horizon, the latest snapshot received by the cutoff is used, with a maximum age of 90 seconds. Cancelled or missing estimates and already-departed events are excluded. The on-time comparator uses exactly the same departures. Each horizon can have a different cohort, so differences across horizons are not evidence that error improves with lead time.

## Matching and limitations

Matching requires exact route, stop number, scheduled date/time and destination, with only case/whitespace normalization. Ambiguous outcomes or multiple live events sharing a match key are excluded. The City records have no trip ID, so this is a conservative record linkage, not an independently verified trip-ID join.

Clock-only trip snapshots are not evaluated here. Unmatched departures are preserved in the match ledger; they are not assigned zero delay. Publication coverage may be incomplete, and missing outcomes are not necessarily random. The City warns that measured records can contain missing data and GPS errors.

Only **8 distinct departures** contribute across the scored horizons. This single-evening sample is an initial accuracy check, not evidence of typical route-wide performance. Some departures share a trip and some appear at several horizons. The historical model has not been compared on this sample, so these numbers do not establish an improvement over it.

## Reproduction

```bash
.venv/bin/python -m src.evaluate_live --run-directory reports/collector_20min_20260915T002352Z \
  --outcomes reports/live_evaluation_20260914/outcomes.json --output-directory reports/live_evaluation_20260914
```

[Measured City dataset](https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u/about_data). The exact source query and retrieval time are saved in `source.json`; row-level matching and scoring are in `matches.json` and `predictions.json`.

All selected API estimates at 10, 20 minutes ahead equal the scheduled departure. Their scores therefore match the on-time baseline exactly; these rows do not demonstrate a benefit from live updates.
