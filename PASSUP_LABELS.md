# Current grouping: two-hour window, all seasons pooled (`route-stop-destination-daytype-2hour-v1`)

Departure timing and pass-up risk use the same groups: exact route, boarding stop, normalized destination (direction), weekday/weekend and a fixed two-hour Winnipeg local window. Windows start on even hours and are keyed by that hour: `16` covers 16:00–17:59, `18` covers 18:00–19:59. Season is no longer part of the group; July 2025–September 2026 history is pooled. Statistics are recalculated from individual visits and matched reports, not by averaging hourly or seasonal summaries. Evidence thresholds are unchanged: timing needs 30 visits across 5 dates; pass-up categories use `reported-pattern-v2`: every gate below except the dropped unresolved-report rule. Both manifests record `grouping` and `groupWindowMinutes: 120`, and the backend refuses artifacts with any other grouping (timing Unavailable, risk Unknown), so older one-hour or seasonal files cannot be matched by mistake. Sections below that mention season or one-hour bins describe earlier releases.

Build of September 26, 2026 (`reports/history-2hour-20260926`, same 13 archives + recent CSV, 46,948,677 retained visits, coverage July 1, 2025–September 24, 2026, 72 routes):

- 83,568 departure-timing groups (previous hourly seasonal build: 341,722 smaller groups).
- Pass-up categories (`reported-pattern-v2`): 70,544 Low, 28 Medium, 0 High, 23,035 Unknown (22,983 insufficient history/persistence, 52 matching-sensitive). Previous hourly seasonal build (v1): 150,266 Low, 30 Medium, 0 High.
- Policy v2 (approved by Linpu, September 26, 2026) drops the v1 rule that marked every group near an unresolved report Unknown. With two-hour, all-season groups that rule had blocked 998 of the 1,547 groups with any matched report, leaving no Medium labels. Unresolved reports are now simply not counted; they remain in the audit (`noUnambiguousNearbyStop` 4,930, `noUnambiguousTimedVisit` 3,905). All other gates are unchanged: minimum evidence, zero-report minimum, score boundaries, report-date persistence and agreement across the three matching profiles. The backend accepts only v2 risk artifacts; a v1 file gives Unknown.
- Test trip 77 Westbrook St → 70 Thatcher Dr, Saturday September 26, 2026, 16:40 (5 plans, 9 rides): timing now available on 9/9 rides (before: 4/9, every BLUE ride Unavailable); pass-up label on 9/9, all Low under both v1 and v2 (before: 0/9, all Unknown). BLUE at stop 10638 toward University of Manitoba, weekend 16:00–17:59: 121 visits, median 4.4 min late. In the app the rides read: BLUE 37% not on time (blue); F8 18% (green) or 46% (red, 38% left early); F9 31% (blue) or 50% (red, 40% left early); no pass-up warning on any ride.

> Current expanded data build: see [HISTORICAL_DATA.md](HISTORICAL_DATA.md). It supersedes the initial snapshot counts and flat-file build instructions below; the category thresholds are unchanged.

# Historical pass-up categories, version 1

The app displays one of Low, Medium, High or Unknown beside each ride. These are relative historical **reported** pass-up patterns, not measured boarding-failure probabilities. Low does not guarantee boarding. Operator under-reporting, incomplete departure telemetry and approximate stop/trip matching remain limitations. No precise percentage is exposed.

## Rebuild

Run the extended audit against the same departure CSV used for the timing artifact:

```sh
python3 src/audit_passups.py --departures Recent_Transit_On-Time_Performance_Data_20260925.csv --passups Transit_Pass-ups_20260925.csv --output reports/passup-labels-20260925
python3 -m unittest discover -s src -p 'test_passup_policy.py'
python3 -m unittest discover -s src -p 'test_audit_passups.py'
```

Copy `passup-risk.json` and `passup-risk.report.json` into the backend's `data/` directory, alongside `reliability.json`. Restart the backend. Docker already includes `data/`. `PASSUP_RISK_DATA_PATH` can override the risk artifact location. The backend requires the cutoff and departure-source SHA-256 to match the timing artifact. The existing trip-date, freshness, direction and sample checks still apply, and the risk manifest's `grouping` must match. Missing or incompatible risk data gives Unknown without removing departure timing.

## Evidence and policy

Use exact route, stop, destination, two-hour scheduled window and weekday/weekend (season was dropped in the current release). Do not borrow evidence from another direction, stop or window. Count distinct matched visits, not button presses; exclude duplicate/conflicting departure identities as in the audit. The denominator is recorded visits, not a claim of complete actual service coverage.

Evaluate three matching profiles independently, deduplicating report matches per visited bus-stop record:

| Profile | Maximum distance | Maximum time difference |
|---|---:|---:|
| Strict | 60 m | 120 seconds |
| Intermediate | 100 m | 180 seconds |
| Broad | 150 m | 300 seconds |

Every profile requires at least 30 m spatial separation from the next candidate and 60 seconds temporal separation from the next candidate. Stops with unstable coordinates are excluded. A report that cannot be assigned to one stop and one visit under even the broad profile is left out of the counts and recorded in the audit. Since `reported-pattern-v2` it no longer marks nearby groups Unknown (v1 did, for the report hour and adjacent hours).

Minimum evidence: 60 recorded visits across 10 distinct dates. For zero-report groups, require at least 120 visits across 10 dates before allowing Low; zero reports alone is insufficient.

Internal score = distinct matched reported visits / (recorded visits + 50). The extra 50 is an explicit small-sample penalty, not extra observed visits or a calibrated probability model. Fixed network-wide boundaries:

- Low: score below 0.010, subject to sample and uncertainty gates.
- Medium: score at least 0.010 but below 0.025, with matched reports on at least 2 distinct dates.
- High: score at least 0.025, with matched reports on at least 3 distinct dates.
- Unknown: insufficient sample/persistence, unstable stop coordinates, or disagreement between matching profiles.

A high score with insufficient persistence becomes Unknown, not Low. The score/category must agree across all three profiles. Thresholds are provisional engineering choices, frozen as `reported-pattern-v1` and carried unchanged into `reported-pattern-v2`, which only removed the unresolved-nearby-report rule. They were informed by the initial audit's 174 positive groups with at least 60 visits and 10 dates: median penalized score 0.008, 75th percentile 0.0117, 90th percentile 0.0224. They are not claimed to be calibrated or independently validated. Do not retune them automatically on each refresh or separately by route. Future changes require a version update, evidence review and corresponding backend validation tests.

## Outputs and UI

`passup-risk.json` contains supported group labels and their evidence. Omitted groups mean Unknown, never Low. `passup-risk.report.json` records label/exclusion counts, source hashes and policy. The original audit outputs remain available; their experimental percentages remain unpublished. The backend validates the label against all exported profile counts before serving it.

The ride UI (updated September 26, 2026) shows a departure line and, only for Medium or High, a pass-up warning:

> Departure: 46% not on time (>1 min early / >5 min late) · 38% left early
> Historical pass-up risk: ⚠️

Not on time = `earlyShare + lateShare`, using the existing window (more than 60 s early or more than 300 s late); no rebuild was needed. The "· N% left early" note appears when at least 20% of departures left more than 1 min early, because early departures are how riders miss the bus. The percentage (rounded first, so the color matches the number shown) is colored green below 20%, blue from 20% to 45%, red above 45% (`NOT_ON_TIME_BANDS` in `lib/reliability.ts`); with the 5-minute window that makes roughly the best third of groups green and the worst fifth red. Only the number is colored; all six shades meet WCAG AA contrast on the light and dark card backgrounds, and the accessibility label states the level (mostly on time / sometimes off schedule / often off schedule). The pass-up line is omitted for Low and Unknown; its accessibility label names the level (Medium or High). A 3-minute late cutoff was considered. It would label about 38% of a typical group not on time instead of 26%, so it is available if wanted, but it needs a new share in the artifact. No raw counts, pass-up score or pass-up percentage is displayed. Older backends and malformed labels fall back to Unknown. Arrival timing is not inferred from departure history.

The earlier audit's recommendation to retain Unknown everywhere is superseded by the user's approval to publish these **descriptive categories**. The unanswered City questions still matter for future probability estimates and validation; no message has been sent to Transit.

## Initial all-route build

For the August 8–September 24, 2026 source snapshot: 1,538 groups qualify as Low, 7 as Medium, none as High, and 275,087 remain Unknown. These are route/stop/destination/day-type/season/hour groups, not numbers of routes or passengers. All 1,545 labeled groups are also present in the departure-timing artifact. Many groups have only a few visits; the gates intentionally do not assign them a category. High remains implemented and tested; thresholds were not relaxed to force a High example into this release.

Example available for a September weekday: BLUE, stop 10639, toward University of Manitoba, 16:00–16:59: Medium. This is a reproducible artifact lookup, not a claim of live crowding.
