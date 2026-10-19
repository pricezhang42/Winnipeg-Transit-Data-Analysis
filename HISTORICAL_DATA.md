# Historical departure timing and pass-up report evidence

`src/build_history.py` is the sole current reliability artifact builder. It imports
monthly ZIP/recent CSV data, cleans recorded departures, matches pass-up reports
to stops, and exports per-route artifacts. See [report-time counting and policy
review](REPORT_TIME_PASSUPS.md) for the pass-up evidence contract.

## Inputs and grouping

- 13 monthly departure ZIPs, July 2025–July 2026, plus the September 25 recent CSV.
- Full-bus reports from `Transit_Pass-ups_20260925.csv` on dates represented in
  the retained departure data. Wheelchair-user reports are outside this metric.
- Network-overhaul cutoff: June 29, 2025. The provided departure files start July 1;
  the builder does not invent missing dates.
- Exact route, stop, normalized destination, weekday/weekend and two-hour Winnipeg
  local window. Windows start on even hours; 16 covers 16:00–17:59. All seasons
  are pooled. Grouping ID: `route-stop-destination-daytype-2hour-v1`.

## Reproduce

Python 3.9+; history builds need only the separate DuckDB dependency:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-history.txt
.venv/bin/python src/build_history.py \
  --departures data/on_time_performance_*.zip Recent_Transit_On-Time_Performance_Data_20260925.csv \
  --passups Transit_Pass-ups_20260925.csv \
  --work data/history-work \
  --output reports/history-report-time-20261018
.venv/bin/python -m unittest discover -s src -p 'test_*history*.py'
```

The working database is several GB and stays outside version control. Import
extracts one ZIP at a time and uses a 1 GB DuckDB memory budget with two threads.
Use `--skip-import` only with a completed working database and its matching
`source-report.json`, after verifying source fingerprints. It reruns matching and
export, not source ingestion. Use `--resume-import` only after a completed raw
import with its matching manifest. Raw inputs are never edited.

## Cleaning

Exclude pre-overhaul rows, invalid identity/time/deviation fields, and deviations
beyond seven days. Collapse exact source duplicates; drop conflicting source IDs.
Keep records whose service day agrees with the calendar. Collapse natural visit
identities (route, stop, destination, scheduled timestamp), excluding conflicting
deviations. Audit counts and daily source coverage are retained in the reports.

A recorded departure remains a proxy observation, not a verified GTFS trip or
complete service census. Missing observations are not cancellations. Timing
minimums describe coverage within recorded data, not coverage of scheduled service.

## Timing statistics

The source deviation is positive when early; `delay = -deviation`. Timing uses
recorded departures within one hour of schedule. Early is more than 60 seconds
early; late is more than 300 seconds late. The boundaries themselves are within
the on-time window. Before-schedule share also includes departures up to one
minute early.

Daily counts supply group totals and cluster-robust share intervals. A group needs
at least 30 observations across five dates. Quantiles (p10, median, p90) come from
individual filtered offsets, not averages of daily/seasonal quantiles. Timing SQL
uses named columns. Connections close on failure.

Fresh imports no longer create obsolete visit-ID or actual-departure timestamp
columns used only by the removed pass-up visit matcher. Previously imported
working databases can still be reused; extra stored columns are ignored and group
keys are recomputed for export.

## Current output

The retained build is [history-report-time-20261018](reports/history-report-time-20261018/):

- `reliability.json`, `reliability.report.json`, `reliability-routes/`: departure timing.
- `passup-report-counts.json`, `passup-report-counts.report.json`,
  `passup-report-routes/`: report counts, dates and departure exposure context.
- `passup-audit.json`, `build-comparison.json`, `passup-policy-review.json`: matching
  totals, validation and explicitly unapproved category comparisons.

The cleaned source has 46,948,677 recorded departures across 72 routes, covering
July 1, 2025–September 24, 2026. Timing has 83,568 supported groups. Stop matching
retains 7,760 / 10,018 / 11,487 reports under 60 / 100 / 150 m profiles, grouped by
report timestamp; there are no temporal profiles or visit assignments. All timing
statistics matched the previous build during validation. Comparison baselines
needed to understand the change are embedded in `build-comparison.json`.

Only current manifests, audits and comparison reports are versioned here; route
shards are retained locally and ignored. Superseded reliability exporters, their
visit-based policies/tests/docs, and previous reliability report snapshots were
removed. They remain recoverable in Git history. Separate modeling, live-capture
and visualization workflows remain documented in README and dashboard docs.

## Backend integration boundary

The existing backend still consumes visit-based `passup-risk.json` under policy
`reported-pattern-v2`. The new report-count schema deliberately has a different
filename/evidence unit and must not be copied into that old contract. Category
selection and compatible backend integration remain pending. The current rebuild
has not replaced backend artifacts or been deployed.

Source coverage still ends September 24, 2026. The backend's current 30-day
freshness/trip-horizon checks and 2026 calendar restrictions remain relevant;
rebuilding identical inputs does not extend that coverage.
