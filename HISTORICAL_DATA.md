# Current grouping: two-hour window, all seasons pooled (`route-stop-destination-daytype-2hour-v1`)

Departure timing and pass-up risk use the same groups: exact route, boarding stop, normalized destination (direction), weekday/weekend and a fixed two-hour Winnipeg local window. Windows start on even hours and are keyed by that hour: `16` covers 16:00–17:59, `18` covers 18:00–19:59. Season is no longer part of the group; July 2025–September 2026 history is pooled. Statistics are recalculated from individual visits and matched reports, not by averaging hourly or seasonal summaries. Evidence thresholds are unchanged: timing needs 30 visits across 5 dates; pass-up categories use `reported-pattern-v2`: every gate below except the dropped unresolved-report rule. Both manifests record `grouping` and `groupWindowMinutes: 120`, and the backend refuses artifacts with any other grouping (timing Unavailable, risk Unknown), so older one-hour or seasonal files cannot be matched by mistake. Sections below that mention season or one-hour bins describe earlier releases.

Build of September 26, 2026 (`reports/history-2hour-20260926`, same 13 archives + recent CSV, 46,948,677 retained visits, coverage July 1, 2025–September 24, 2026, 72 routes):

- 83,568 departure-timing groups (previous hourly seasonal build: 341,722 smaller groups).
- Pass-up categories (`reported-pattern-v2`): 70,544 Low, 28 Medium, 0 High, 23,035 Unknown (22,983 insufficient history/persistence, 52 matching-sensitive). Previous hourly seasonal build (v1): 150,266 Low, 30 Medium, 0 High.
- Policy v2 (approved by Linpu, September 26, 2026) drops the v1 rule that marked every group near an unresolved report Unknown. With two-hour, all-season groups that rule had blocked 998 of the 1,547 groups with any matched report, leaving no Medium labels. Unresolved reports are now simply not counted; they remain in the audit (`noUnambiguousNearbyStop` 4,930, `noUnambiguousTimedVisit` 3,905). All other gates are unchanged: minimum evidence, zero-report minimum, score boundaries, report-date persistence and agreement across the three matching profiles. The backend accepts only v2 risk artifacts; a v1 file gives Unknown.
- Test trip 77 Westbrook St → 70 Thatcher Dr, Saturday September 26, 2026, 16:40 (5 plans, 9 rides): timing now available on 9/9 rides (before: 4/9, every BLUE ride Unavailable); pass-up label on 9/9, all Low under both v1 and v2 (before: 0/9, all Unknown). BLUE at stop 10638 toward University of Manitoba, weekend 16:00–17:59: 121 visits, median 4.4 min late. In the app the rides read: BLUE 37% not on time (blue); F8 18% (green) or 46% (red, 38% left early); F9 31% (blue) or 50% (red, 40% left early); no pass-up warning on any ride.

# Expanded post-overhaul history

The current build combines all 13 `data/on_time_performance_YYYY_MM.zip` archives (July 2025 through July 2026) and `Recent_Transit_On-Time_Performance_Data_20260925.csv`. The cutoff is June 29, 2025; the provided files begin in July, so June 29–30 are not reconstructed. Older pass-up reports are only used on dates with retained departure observations.

## Reproduce

Use Python 3.9+ and the separate history dependency list:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-history.txt
.venv/bin/python src/build_history.py \
  --departures data/on_time_performance_*.zip Recent_Transit_On-Time_Performance_Data_20260925.csv \
  --passups Transit_Pass-ups_20260925.csv \
  --work data/history-work \
  --output reports/history-2hour-20260926
.venv/bin/python -m unittest discover -s src -p 'test_build_history.py'
```

Keep the working database outside version control; it occupies several GB. ZIPs are streamed through one temporary extracted CSV at a time. DuckDB has a 1 GB memory budget and two worker threads. `--skip-import` can repeat export after a completed import; it uses the existing database and `source-report.json`, so do not use it for new or changed departure inputs. Rebuild normally when sources change. Raw files are never edited.

Copy the following output set together into the backend's `data/` directory and restart the backend:

- `reliability.json`, `reliability.report.json`, and the complete `reliability-routes/` directory
- `passup-risk.json`, `passup-risk.report.json`, and the complete `passup-risk-routes/` directory

Schema 2 manifests reference per-route files. The backend supports the old schema as well, lazily reads only the routes needed by a request, and bounds its file cache to four entries / 12 MB of serialized data. Missing files fail open to unavailable timing or Unknown risk. Docker includes the full `data/` tree. The two manifests must have the same departure-source bundle fingerprint and coverage cutoff.

## Cleaning and comparability

- Parse both ISO archive timestamps and the recent CSV's month-name timestamps. Ignore all pre-overhaul observations.
- Remove exact duplicate source records across files; exclude source IDs with conflicting timing/identity information. Collapse natural visit identities (route, stop, destination, scheduled time); exclude conflicting deviations for the same identity. Natural identities remain proxies for trips because vehicle/trip IDs are absent.
- Exclude invalid rows, deviations over seven days, and rows whose reported service day disagrees with the calendar. Records marked Holiday are excluded; this does not claim to recognize every possible special-service date. Source rows outside the ordinary calendar comparison are counted in the audit.
- Departure timing uses observations within one hour of schedule; pass-up denominators retain larger deviations up to the structural validity bound. Do not remove very late visits from the denominator merely for being late.
- Group by exact route, stop, normalized destination, weekday/weekend and two-hour window; all seasons are pooled (earlier builds separated season and used one-hour bins). Old destination labels are not fuzzily merged into current destinations. No historical timetable is inferred from the current GTFS feed.
- Use stop coordinates and route/destination membership observed on each report's date. Stops with unstable coordinates within that date are excluded. This avoids using one latest stop location across a year of history. Matching still lacks shared trip identifiers and manual ground truth; local timestamps at the repeated daylight-saving hour remain an ambiguity.
- Keep the approved Low/Medium/High thresholds and all three matching-sensitivity checks unchanged (policy `reported-pattern-v2` only dropped the unresolved-nearby-report rule). No rebalance was applied. Zero-report groups still require adequate history and uncertain matches remain Unknown.

The departure artifact retains exact observed median and p10/p90 offsets. Stored share intervals now use a daily-cluster variance calculation with a normal 95% approximation rather than 100 bootstrap repetitions, allowing aggregation of the longer history. They are descriptive uncertainty estimates, not calibrated future-arrival intervals, and are not shown in the two-line UI. No new predictive holdout claim is made; earlier pilot evaluation numbers do not evaluate this expanded release.

## Coverage and operational limits

`reliability.report.json` records all input filenames and SHA-256 hashes, cleaning counts and every retained date's visit count. `passup-risk.report.json` records the new category distribution. The import does not fill missing days or infer zero pass-ups on uncovered days. Pass-up records outside observed departure dates are excluded from the denominator-based comparison.

The backend's 30-day data freshness and trip-horizon rules still apply. Having winter history does not turn a months-ahead itinerary into a supported live estimate: refresh recent data as the season progresses. Source coverage extends through September 24, 2026; keep the backend's calendar rules updated for future years. The app's existing two summary lines and outside-time calculation are unchanged.

## Previous result (hourly, seasonal build of September 25, 2026)

- 48,307,022 source departure rows; 46,948,677 retained recorded visits.
- Usable coverage: July 1, 2025–September 24, 2026, across 72 route identifiers.
- 341,722 supported departure groups, including 124,265 winter groups.
- 16,049 full-bus reports on covered dates; one has invalid coordinates, leaving 16,048 spatial candidates. The three profiles match 4,411 / 5,763 / 6,910 distinct candidate visits respectively. Reports remain approximate matches, not ground truth.
- 150,266 Low groups, 30 Medium, zero High; 323,309 comparison groups remain Unknown. Categories refer to stop/direction/hour/season combinations, not numbers of routes. Thresholds were not changed to rebalance labels.
- Dates within the coverage range with no retained observations: August 4, 2025; February 16, May 18, July 1, August 1–7, and September 6–8, 2026. These gaps can result from absent inputs or the cleaning/service-day filters; the report does not claim they are all missing downloads. There is no June 2025 archive in the supplied folder.

Validation: ZIP/CSV timestamp parsing, overlap removal, conflicting source/visit identities, cutoff filtering and winter/summer separation tested with fixtures. Backend loader tests pass for both artifact schemas, missing files and path containment. All 341,722 timing groups and all 150,296 supported labels passed export/lookup checks. Example integrated lookup: route 662, stop 60188, University of Manitoba, September weekday 16:00–16:59 returns Medium. No emulator test or production deployment was performed for this data refresh.
