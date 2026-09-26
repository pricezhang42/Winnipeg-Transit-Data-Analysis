# Current grouping: two-hour window, all seasons pooled (`route-stop-destination-daytype-2hour-v1`)

Departure timing and pass-up risk use the same groups: exact route, boarding stop, normalized destination (direction), weekday/weekend and a fixed two-hour Winnipeg local window. Windows start on even hours and are keyed by that hour: `16` covers 16:00–17:59, `18` covers 18:00–19:59. Season is no longer part of the group; July 2025–September 2026 history is pooled. Statistics are recalculated from individual visits and matched reports, not by averaging hourly or seasonal summaries. Evidence thresholds are unchanged: timing needs 30 visits across 5 dates; pass-up categories use `reported-pattern-v2`: every gate below except the dropped unresolved-report rule. Both manifests record `grouping` and `groupWindowMinutes: 120`, and the backend refuses artifacts with any other grouping (timing Unavailable, risk Unknown), so older one-hour or seasonal files cannot be matched by mistake. Sections below that mention season or one-hour bins describe earlier releases.

Build of September 26, 2026 (`reports/history-2hour-20260926`, same 13 archives + recent CSV, 46,948,677 retained visits, coverage July 1, 2025–September 24, 2026, 72 routes):

- 83,568 departure-timing groups (previous hourly seasonal build: 341,722 smaller groups).
- Pass-up categories (`reported-pattern-v2`): 70,544 Low, 28 Medium, 0 High, 23,035 Unknown (22,983 insufficient history/persistence, 52 matching-sensitive). Previous hourly seasonal build (v1): 150,266 Low, 30 Medium, 0 High.
- Policy v2 (approved by Linpu, September 26, 2026) drops the v1 rule that marked every group near an unresolved report Unknown. With two-hour, all-season groups that rule had blocked 998 of the 1,547 groups with any matched report, leaving no Medium labels. Unresolved reports are now simply not counted; they remain in the audit (`noUnambiguousNearbyStop` 4,930, `noUnambiguousTimedVisit` 3,905). All other gates are unchanged: minimum evidence, zero-report minimum, score boundaries, report-date persistence and agreement across the three matching profiles. The backend accepts only v2 risk artifacts; a v1 file gives Unknown.
- Test trip 77 Westbrook St → 70 Thatcher Dr, Saturday September 26, 2026, 16:40 (5 plans, 9 rides): timing now available on 9/9 rides (before: 4/9, every BLUE ride Unavailable); pass-up label on 9/9, all Low under both v1 and v2 (before: 0/9, all Unknown). BLUE at stop 10638 toward University of Manitoba, weekend 16:00–17:59: 121 visits, median 4.4 min late. In the app the rides read: BLUE 37% not on time (blue); F8 18% (green) or 46% (red, 38% left early); F9 31% (blue) or 50% (red, 40% left early); no pass-up warning on any ride.

> Current expanded data build: see [HISTORICAL_DATA.md](HISTORICAL_DATA.md). It supersedes the initial snapshot counts and flat-file build instructions below; the category thresholds are unchanged.

# Historical departure reliability

Routes: every route present in the departure source, with no route whitelist. Individual vehicles cannot be distinguished because the source lacks vehicle identifiers. This release describes observed departures, not live predictions, arrival times, trip success, or a calibrated probability that the next bus will pass a passenger.

Build from the two City open-data CSVs with Python 3 (standard library only):

```sh
python3 src/export_reliability.py --departures Recent_Transit_On-Time_Performance_Data_20260925.csv --passups Transit_Pass-ups_20260925.csv --output exports/reliability.json
python3 -m unittest discover -s src -p 'test_export_reliability.py'
```

Copy the generated JSON to the backend's `data/reliability.json`, restart/redeploy the backend, and retain the `.report.json` audit with the export. No raw CSV is shipped to the phone or server. The backend loads once at startup; `RELIABILITY_DATA_PATH` can override its location. Docker includes `data/`. No database migration is required.

Group identity (initial release) was exact route, boarding stop, normalized destination, calendar weekday/weekend, season and Winnipeg local one-hour bin; the current release drops season and uses two-hour windows (see top). Normalization trims, lowercases and removes leading “to ”; API variant names also remove the exact route display-name prefix before “to ”; unmapped destination names remain unavailable. Summer is June–August, winter November–March, remaining months transition. At least 30 observations on 5 dates are required; there is no cross-stop, direction, seasonal or route fallback. Source service days inconsistent with the calendar are excluded. The backend conservatively excludes listed 2026 holidays and other calendar years; refresh this calendar for future releases.

Source positive Deviation means early: exported delay is its negative. Early is less than -60 seconds; late greater than 300; the inclusive interval between is “within window.” Before-schedule share also counts departures up to one minute early. Median and p10/p90 describe the observed offsets. Share intervals in the stored artifact use 100 whole-date bootstrap samples, seed 20260925; they are uncertainty descriptions, not calibrated future guarantees. Duplicate row IDs, invalid rows and absolute deviations over an hour are excluded and audited.

Nearby full-bus report counts use same route and destination, nearest historical stop within 60 m with at least 30 m separation from the next candidate, and only dates observed in that exact group. Coordinates are approximate and assignments have not been manually validated. No departure denominator or passenger-exposure denominator has been established for pass-ups. Never turn these counts into percentages; zero is not proof of no risk. Wheelchair reports are excluded from this full-bus metric.

Current source coverage is August 8–September 24, 2026; no winter observations. The final 7 dates are a retrospective holdout, now inspected. See reliability.report.json for metrics and audit. The all-route export contains 72 routes and 44,568 eligible groups across 63 routes. Nine routes have no group meeting the evidence minimum; they remain supported but display Unavailable. Holdout matched coverage was 28.9%; middle-80 interval coverage was approximately 74.6% on matched holdout observations, reinforcing descriptive-only labeling. Sparse stop/hour combinations stay unavailable. Fresh prospective evaluation and manual stop matching remain gates before forecasting claims.

Backend rejects trip dates on/before the artifact cutoff (avoids using future observations for historical trips), more than 30 days after cutoff, or artifacts older than 30 days. Refresh data regularly. Missing/invalid enrichment never blocks itinerary delivery. Each ride shows a not-on-time percentage and, only for Medium/High, a pass-up warning (see PASSUP_LABELS.md). Evidence stays in the backend data and audit report. Outside-time calculation remains walking plus unsheltered waiting.

Local validation: backend `npm run typecheck`, `node --import tsx --test src/reliability.test.ts`; app `npx jest --runInBand --watch=false`. Production deployment and actual emulator verification are separate steps.

## Try it locally

Start the companion backend with `npm run dev`, then restart Expo. Android emulator defaults to `http://10.0.2.2:8787`. If `.env.local` sets `EXPO_PUBLIC_BACKEND_URL` to a hosted backend, use a local URL for development or deploy the companion backend before expecting reliability in the app. Expand any ride to see the departure line (and a pass-up warning when Medium/High); sparse or missing history displays Unavailable. The live sample checked was F8 at stop 60129 toward Glenway at 13:00 on September 25, 2026.

Validation: real upstream plan responses enriched successfully for F8 and BLUE; F6 now also matched successfully with the all-route artifact. Backend build and lookup tests passed. Existing Map screen TypeScript errors and the legacy StyledText snapshot-test teardown issue are unrelated to this change.

## Requirements before publishing pass-up percentages

Define the target first: the chance a bus visit leaves at least one passenger behind differs from the chance an individual rider cannot board. For a bus-visit rate, count distinct verified full-bus pass-up visits divided by all comparable actual visits, including successful boardings and buses that pass without stopping. Scheduled trips alone are not an observed denominator; existing departure records must be audited for coverage of skipped stops, missing telemetry and duplicates before use.

Required data and checks:

- Stop, trip/service date, route/direction and actual visit identifiers that join pass-up reports to service observations reliably. Verify approximate GPS matches manually against known outcomes.
- Reporting-completeness validation: an absent operator report cannot automatically be treated as a successful boarding. Confirm how consecutive skipped stops and duplicate reports are logged.
- Enough independent dates and events for each route/stop/time/day-type/season; record uncertainty and suppress sparse estimates. Collect winter and holiday coverage.
- Evaluate on later, untouched dates, check calibration (e.g., groups predicted 20% should experience close to 20% observed outcomes) and stability by route/season. Define and document Low/Medium/High thresholds only after this validation.
- Passenger counts, capacity and boarding/left-behind observations if claiming an individual rider's chance of boarding or vehicle-specific crowding.

Exact percentages remain unavailable. The approved descriptive-category policy now supports Low, Medium and High when sample, persistence and matching-stability gates pass; otherwise it displays Unknown. See `PASSUP_LABELS.md`. A future validated result could read `Pass-up risk: High (25%)`, with the percentage tied to a clearly defined outcome.

City documentation: https://info.winnipegtransit.com/en/open-data/pass-ups/ describes operator-button reporting and approximate coordinates; https://info.winnipegtransit.com/en/open-data/on-time-performance describes departures, not arrivals.

Historical pass-up categories are now served from a separate source-matched artifact. See [category policy](PASSUP_LABELS.md) for thresholds, evidence gates and rebuild instructions.
