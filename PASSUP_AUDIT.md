# Pass-up matching and denominator audit

Nearest-stop matching is a reasonable candidate method, not ground truth. Match only to stops observed serving the same route and normalized route destination in the departure source. Opposite directions and unmatched destination names are not merged. Current GTFS is not used to assert historical service. Historical destination alone may not uniquely identify a branch; ambiguous locations are rejected.

Run from the analysis repository:

```sh
python3 src/audit_passups.py \
  --departures Recent_Transit_On-Time_Performance_Data_20260925.csv \
  --passups Transit_Pass-ups_20260925.csv \
  --output reports/passup-audit
python3 -m unittest discover -s src -p 'test_audit_passups.py'
```

This is a separate offline audit and does not change the app's `Pass-up risk: Unknown` display. It uses standard-library Python and a temporary SQLite index; allow several minutes and a few GB of temporary disk space for the current five-million-row file.

## Matching and counting

1. A candidate stop must be within 60 m of the report, at least 30 m closer than the next compatible stop. Exclude a stop if source coordinates move more than 30 m from its first recorded position. This is conservative; moved stops require dated coordinates rather than silently choosing the newest location.
2. Reconstruct measured departure time as scheduled time minus source Deviation. Find an observed visit within 180 seconds of the operator report, with a temporal advantage of at least 60 seconds over the next observed candidate. Times are compared as Winnipeg local clock values; DST repeated-hour ambiguity remains a limitation for future autumn data.
3. Collapse identical route/stop/destination/scheduled-time records. Exclude identities with conflicting measured times. Source IDs and report IDs are also deduplicated. A natural-key collision could represent two real buses; without trip IDs, retain this as a limitation rather than claim exact vehicle counting.
4. Count multiple report IDs matched to the same recorded visit only once in the numerator. Count all valid recorded visits for the same route, stop, destination, scheduled hour, weekday/weekend and season as the candidate denominator. Unlike the departure-timing model, this audit does not remove visits merely because delay exceeds one hour. Rows whose reported service day differs from the calendar are excluded from the comparable denominator.
5. Produce diagnostic rates only for groups with matched reports; missing groups must not be interpreted as zero risk. `experimentalReportedRate` is a fraction with `publishable: false`, not a passenger boarding-failure probability. A single matched report in one recorded visit can yield 100% in the audit and is not a publishable estimate.

Outputs: `audit.json` includes source SHA-256 hashes, exclusion counts, per-route match results, experimental group denominators and distance/time sensitivity. `matches.csv` contains every considered report and its match/rejection reason, nearest candidate distances, and matched scheduled/actual time for manual review. Sensitivity counts are report-level diagnostic counts before collapsing repeated reports per visit. Do not select the tolerance yielding the largest count without independent validation.

## What this audit cannot establish

An unmatched report could mean a delayed button press, inaccurate GPS, a missing departure record, a wrong destination label, or a passed stop that never generated a departure record. Matching alone cannot distinguish these. No report does not prove no pass-up. Observed row counts do not establish complete service coverage; scheduled GTFS stop calls are a comparison baseline, not proof that a bus operated.

Before a percentage is displayed, manually validate a stratified sample of accepted and rejected matches (routes, distances, crowded corridors, times and seasons), confirm skipped-stop logging and reporting completeness with Transit, and evaluate on later untouched data. Request historical trip/vehicle IDs, stop sequence and cancellation/skip flags if available. Individual passenger risk additionally needs boarding and left-behind outcomes.

## Draft questions for Winnipeg Transit / City Open Data

- Does the on-time performance export create a record when a bus passes a scheduled stop without stopping, including when full? What triggers the record: GPS crossing, doors, or another event?
- Does one row represent one unique operated trip at one stop? Can multiple buses share route/destination/stop/scheduled time? Can telemetry retries produce multiple rows, and what does Row ID identify?
- Are departure-record omissions measurable by route and date? Are cancelled, diverted and skipped stop calls identifiable? Is an archive of actual stop visits with trip/vehicle IDs and stop sequence available?
- How are consecutive full-bus pass-ups reported? Can one button press cover several stops, or can multiple presses describe the same visit? What latency is expected between the event and button press?
- Can pass-up reports be joined internally to actual trips/stops, and can a de-identified extract or validation sample be shared?

This is a draft only; no message has been sent.

Sources: [Recent departures](https://data.winnipeg.ca/d/gp3k-am4u), [departure archive](https://data.winnipeg.ca/d/cymk-nyei), [pass-up dataset](https://data.winnipeg.ca/d/mer2-irmb), [City pass-up methodology](https://info.winnipegtransit.com/en/open-data/pass-ups/), [City departure methodology](https://info.winnipegtransit.com/en/open-data/on-time-performance).

## September 25, 2026 audit results

Sources cover August 8–September 24, 2026. Of 5,023,382 departure rows, the audit retained 4,984,828 candidate recorded visits. It collapsed 273 duplicate visit rows, excluded 17,561 conflicting visit identities (18,930 conflicting rows), 92 invalid rows and 1,698 calendar/service-day mismatches. These are natural-key candidate visits, not verified complete trip/vehicle counts. Nine route/destination/stop identities had unstable coordinates.

Of 1,989 full-bus reports on source dates:

| Result | Reports |
|---|---:|
| Matched to a distinct candidate visit | 649 |
| Additional report matched to an already counted visit | 17 |
| No compatible stop within 60 m | 966 |
| Ambiguous nearby stops | 5 |
| Compatible stop, no candidate visit within 180 s | 288 |
| Ambiguous visit timing | 64 |

The 649 distinct candidate matched visits span 427 comparison groups. None is approved for publication. A match is an algorithmic candidate, not a manually verified event. In particular, the 288 missing temporal matches do not establish that passed stops are missing from the departure dataset; button latency, GPS error and telemetry omissions are alternative explanations.

At a fixed 180-second temporal tolerance, the report-level matches before collapsing repeated visits rise from 666 at 60 m to 812 at 100 m, 904 at 150 m, and 978 at 300 m. Increasing tolerance improves coverage but can introduce false stop/trip assignments. Independent validation is needed to choose settings.

Saved files in `reports/passup-audit-20260925/`: `audit.json`, `matches.csv`, and a deterministic 55-row `manual-review.csv` sample covering all result categories (seed 20260925, up to 10 per category). Review outcomes remain blank; no manual truth validation is claimed. CSVs remain subject to the repository's existing CSV ignore rule.

Decision: retain the app's Unknown label while validating stop/trip assignments and requesting confirmation of skipped-stop logging. The current output supports investigation of a historical reported-pass-up rate, not a calibrated risk percentage.
