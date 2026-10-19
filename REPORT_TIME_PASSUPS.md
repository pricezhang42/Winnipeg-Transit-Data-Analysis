# Report-time pass-up categories

Policy `report-scheduled-visit-ratio-v1` was approved for implementation on
October 18, 2026. It replaces the unapproved count-only and penalized-score
candidates. These are descriptive categories, not boarding-failure probabilities.

## Matching and grouping

1. Keep full-bus reports on dates represented in the cleaned departure data;
   deduplicate report IDs and reject invalid coordinates.
2. Match the nearest stop on the same route, destination and report date, using
   average valid coordinates observed that date. Ties use stop ID. There is no
   coordinate-instability filter, runner-up comparison or report-to-visit match.
3. Use the 150 m matches for classification. Retain 60 m and 100 m counts for
   auditing only; their categories do not need to agree.
4. Group reports by their Winnipeg timestamp, route, stop, normalized destination,
   weekday/weekend and fixed two-hour window, pooling seasons. For example, hour
   16 covers 16:00–17:59.
5. Reference visits are cleaned, deduplicated recorded departure observations in
   the corresponding **scheduled-time** group, including visits outside the
   departure-timing statistics' one-hour deviation filter. They are not the full
   GTFS scheduled service count. All available covered dates are used.

## Category rules

Ratio = unique full-bus report IDs / recorded visits grouped by scheduled time.
There is no +50 penalty. Different report IDs may describe one bus, so this ratio
is not a fraction of verified affected visits and is not shown as a probability.

First require 60 recorded visits across 10 distinct dates. Then:

| Category | Rule |
|---|---|
| Low | Ratio below 0.01; zero reports additionally require 120 visits |
| Medium | Ratio at least 0.01 and below 0.025, with reports on at least 2 dates |
| High | Ratio at least 0.025, with reports on at least 3 dates |
| Unknown | Missing/invalid evidence, insufficient visits/dates/persistence, or reports exceeding recorded visits |

A high-ratio group with only two reporting dates is Unknown, not Medium. Zero
reference departures remain in the evidence but cannot produce a category.
Ratios above one are retained as evidence anomalies rather than capped.

`src/report_policy.py` owns the analysis rules and audit. The backend implements
the same versioned rules and validates the complete policy definition in the
manifest. All-group parity checks compare the two implementations.

## Artifacts and API

- `reliability.json` + `reliability-routes/`: unchanged timing statistics. The
  auxiliary `nearbyFullBusReports` field uses report-time-grouped 100 m counts.
- `passup-report-counts.json` + `passup-report-routes/`: schema 3 evidence with
  `stop-report-counts-v1`, `nearest-stop-report-time-v1`, unique-report-ID units,
  reported-local-time grouping, scheduled-local-time reference counts,
  `labelStatus: approved` and the full `policy` definition.
- `passup-report-counts.report.json`: source identity and matching audit.
- `passup-policy-report.json`: approved policy, category totals and Unknown reasons.

The backend loads this separate artifact set and serves ride-level `passupReports`
and `passupRisk`. The app uses the independent category even if departure timing
is unavailable. Existing nested visit-based risk and its files remain available
for older clients during migration; no new report counts enter that old reader.
The app continues to show a warning only for Medium/High, with the category in
accessibility text, and displays no numeric pass-up ratio.

## Current results

The existing validated evidence was reused; neither raw-data cleaning, matching
nor departure timing changed. Source coverage remains July 1, 2025–September 24,
2026: 46,948,677 recorded departures, 72 routes and 83,568 timing groups.

Of 199,249 original reports, 10,991 are non-full-bus and 172,209 are outside covered
dates. Of the remaining 16,049 reports, one has invalid coordinates and 4,561 have
no stop within 150 m. Matches: 7,760 at 60 m, 10,018 at 100 m, 11,487 at 150 m.
There are 93,609 evidence groups; 2,008 contain reports and two have no reference
visits.

| Low | Medium | High | Unknown |
|---:|---:|---:|---:|
| 70,428 | 161 | 35 | 22,985 |

Unknown reasons: 15,790 insufficient reference history; 7,177 insufficient
zero-report history; 18 insufficient reporting-date persistence. No eligible
group has a ratio above one. These counts describe the rules' output, not accuracy.

The current output is `reports/history-report-time-20261018/`. Follow
[HISTORICAL_DATA.md](HISTORICAL_DATA.md) to rebuild, optionally reusing a verified
working database with `--skip-import`. Install complete manifests and shards and
restart the backend. Code/artifact preparation does not deploy a hosted server;
rebuilding unchanged inputs does not extend data freshness.
