# BLUE data preparation and departure-delay baseline

Prepared from `Recent_Transit_On-Time_Performance_Data_20260913.csv`. One record represents a scheduled stop departure, not a complete bus trip.

## Data coverage

The source contains **4,955,221 records** covering 2026-07-25 00:00:00 through 2026-09-12 23:14:00. The prepared BLUE subset retains **403,511 records**, 111 stops, and 4 destinations.

Missing source dates: 2026-09-06, 2026-09-07, 2026-09-08.
Missing BLUE dates: 2026-09-06, 2026-09-07, 2026-09-08, 2026-09-09.
See daily counts in `data_quality.json` and the coverage plot. Missing observations are not evidence of cancelled buses. Partial collection can affect other dates too; nonzero counts do not establish complete coverage.

## Parsing and quality decisions

- Parsed 53,203 source deviations containing thousands commas. After parsing, 0 source deviations and 0 scheduled times are invalid.
- Normalized `delay_seconds = -deviation_seconds`: positive means late, negative means early. Original deviation, scheduled-time text, location, Row ID, and source record number are retained.
- Scheduled timestamps preserve local wall-clock time, interpreted as America/Winnipeg. The CSV supplies no offset. `scheduled_date` is the calendar date, not an inferred transit service date; `day_type` is retained separately.
- Flagged 664 BLUE records with absolute delay greater than 3,600 seconds. This is an exploratory review threshold, not an official validity rule. There is no evidence sufficient to correct these values, so all remain in the prepared file.
- Of those extreme records, 486 indicate early departures and 178 indicate late departures. The most extreme early value is 6.44 hours before schedule. Example Row IDs, stops, destinations, and timestamps are retained in `data_quality.json` for inspection. These flags identify suspicious measurements; they do not establish a specific GPS, clock, or scheduling fault.
- Flagged 46 additional records with identical source fields except Row ID. These are duplicate candidates, not confirmed errors: the CSV has no trip identifier. `duplicate_of_row_id` identifies the first matching source record, and all copies remain available.
- `baseline_eligible` selects 402,801 records with valid required fields, destination, and day type, without extreme-delay or duplicate-candidate flags. Invalid locations are flagged separately because the baseline does not use coordinates.
- The baseline fits eligible training rows. Primary evaluation includes **all parseable validation/test observations**, including extreme delays and duplicate candidates. A second evaluation shows the unflagged subset with the same frozen predictions.

## Chronological experiment

| Split | Local interval, end excluded | Retained rows | Eligible rows |
|---|---|---:|---:|
| train | 2026-07-25 to 2026-08-17 | 186,666 | 186,349 |
| validation | 2026-08-17 to 2026-08-24 | 62,446 | 62,301 |
| test | 2026-08-24 to 2026-08-31 | 64,669 | 64,574 |

Dates after the test interval are reserved for a subsequent check and have no baseline scores. Delay-by-hour, delay-by-stop, and distribution plots use eligible training rows only.

The predictor takes the training median for stop, destination, day type, and hour, with at least 20 observations per group. Sparse or unseen groups fall back through stop/destination/day type, stop/destination, destination/day type/hour, destination/day type, destination, and finally the global training median (49.0 seconds). Destination is a useful grouping field, not a unique trip or route-variant identifier. No evaluation labels enter these lookups, and no settings were selected using test performance.

## Baseline results

Errors are in seconds. Lower MAE is better; higher within-two-minutes coverage is better.

| Split / population | Observations | On-time MAE | Global-median MAE | Grouped-median MAE | Grouped within ±2 min |
|---|---:|---:|---:|---:|---:|
| validation / all valid | 62,446 | 147.3 | 138.2 | 125.8 | 68.1% |
| validation / unflagged | 62,301 | 133.9 | 124.8 | 112.4 | 68.2% |
| test / all valid | 64,669 | 141.6 | 131.5 | 118.8 | 69.5% |
| test / unflagged | 64,574 | 130.2 | 120.2 | 107.3 | 69.6% |

`baseline_metrics.json` also includes RMSE, median and 90th-percentile absolute error, signed bias, fallback usage, and test errors by destination and date. `baseline_predictions.parquet` contains the observation-level validation/test predictions.

## What this result means

This is a historical-pattern departure-delay baseline for observed stop events during this summer snapshot. It does not measure winter performance, identify cancellations, predict arrivals, or use a bus's current delay. Multiple observations can belong to one bus trip, so the row count is not a count of independent trips.

The next experiment is a small supervised regression model using the same available features and a fresh later evaluation period after checking its coverage. Compare against these frozen median lookups. A previous-stop or current-delay model requires reliable trip-linked observations not present in this CSV.

## Files

- `../data/processed/blue_clean.parquet`: every BLUE row, typed fields, quality flags, and split labels.
- `blue_review.parquet`: rows needing review; these are also retained in the clean file.
- `blue_overview.png`: data coverage and training-period patterns.
- `data_quality.json`: source hash, coverage, parsing checks, and row reconciliation.
- `../models/historical_median.json`: training-only lookup tables and fallback rules.

## Source

[City of Winnipeg dataset](https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u). The source documentation warns that transmission and GPS issues can produce missing or erroneous records. A documentation snapshot is saved in `../data/source_metadata.json`.

Source SHA-256: `b81aeac3785cc39bc42e1de8bc5d79ef5227dd6fc41d18a4dba6732bf2a6636a`.
