# Report-time pass-up aggregation

The current `build_history.py` pipeline matches reports only to stops. It does not
identify a bus visit or compare reported time to a departure time.

## Counting

1. Retain full-bus reports on dates represented in the cleaned departure data.
   Deduplicate by report ID and reject invalid coordinates.
2. Find the nearest stop on the same route, destination and report date, using
   average valid coordinates observed on that date. Do not reject coordinate spread
   or compare a runner-up. Exact spatial ties use stop ID.
3. Keep matches within 150 m. Count them separately under spatial-only 60 m,
   100 m and 150 m profiles. There is no temporal tolerance.
4. Group by route, stop, normalized destination, weekday/weekend and the two-hour
   window containing the **report's timestamp** in Winnipeg local time. Seasons
   are pooled. A report at 14:00 belongs to 14:00–15:59 regardless of bus schedule.
5. Count distinct report IDs and distinct report dates. Different IDs remain
   separate even if they might describe one bus; do not call them affected visits.

Recorded departure counts in the corresponding scheduled-time group are retained
as exposure context, not a probability denominator. Reports with no corresponding
scheduled-time group remain in the evidence with zero reference departures.

## Artifacts and compatibility

- `reliability.json` and `reliability-routes/`: unchanged timing schema and statistics;
  the auxiliary `nearbyFullBusReports` field now counts report-time-grouped 100 m reports.
- `passup-report-counts.json` and `passup-report-routes/`: new schema 3 evidence,
  `evidenceVersion: stop-report-counts-v1`, `evidenceUnit: unique-report-id`,
  `groupTimeBasis: reported-local-time`, `referenceTimeBasis: scheduled-local-time`.
- `passup-report-counts.report.json`: coverage, identity and source/matching audit.
- `passup-policy-review.json`: two explicitly unapproved rule comparisons.

All new manifests record `passupMatchingMethod: nearest-stop-report-time-v1`.
No visit IDs, time differences, distinct-visit totals, or temporal profiles are
exported for report matching. The current builder does not emit `passup-risk.json`
or production Low/Medium/High labels for this new evidence.

The existing backend accepts only the older visit-based `reported-pattern-v2`
artifact. It must not consume report counts under `reportedVisits` or silently
reuse its old temporal profiles. Existing backend files remain untouched. Review
new category rules, then implement a versioned backend contract and deploy the
matching artifact set together. Do not copy only the timing manifest over an
older backend data set and mistake its old risk artifact for new report labels.

## Policy review

`report_policy_review.py` compares:

- Legacy score boundaries adapted to reports: reports / (recorded departures + 50),
  with the earlier score boundaries and persistence checks, for comparison only.
- Report-count/persistence rules: Medium at least 2 reports on 2 dates; High at
  least 5 reports on 3 dates, for comparison only.

Both comparisons require the same reference coverage checks (60 departures on
10 dates; zero-report Low needs 120 departures) and agreement across spatial
profiles. These are candidate engineering choices, not approved thresholds or
calibrated percentages. Report-only groups without exposure context remain
Unknown in the comparisons. Neither candidate assigns production labels.

## Rebuild

The October 18 output directory is `reports/history-report-time-20261018/`.
Use the documented full-source build command in `HISTORICAL_DATA.md`, changing
`--output` to that directory. `--skip-import` can reuse the completed cleaned
visits database only after verifying the input fingerprints. The rebuild reuses
data through September 24, 2026; it does not extend data freshness. Superseded visit-based builds and their code are recoverable in Git history;
the retained build comparison embeds the prior matching audit for reference.

## October 18 validation results

- Source: 199,249 reports; 10,991 non-full-bus reports and 172,209 reports outside
  covered departure dates are outside scope. Of 16,049 covered full-bus reports,
  one has invalid coordinates and 4,561 have no stop within 150 m.
- Spatial matches: 7,760 at 60 m; 10,018 at 100 m; 11,487 at 150 m. The broad
  profile retains 3,466 more reports than the previous nearest-visit build.
- 93,609 evidence groups including zero-report reference groups; 2,008 groups
  contain reports. Two positive groups have no scheduled-departure reference.
- All 46,948,677 reference departures are accounted for. All 83,568 timing groups
  retain identical timing statistics. Auxiliary report counts change in 1,301 groups.
- All 29,265 profile/report audit rows were checked against report-time keys and
  unique report IDs. No bus-visit assignment or temporal matching remains.

| Unapproved candidate | Low | Medium | High | Unknown |
|---|---:|---:|---:|---:|
| Legacy score adapted to report counts | 70,446 | 83 | 12 | 23,068 |
| Report count and date persistence | 69,535 | 353 | 370 | 23,351 |

Full audit, comparison and candidate rules are saved under the rebuild directory.
These results show the consequences of the rules, not their accuracy or calibration.
20 relevant tests pass. No backend files have been replaced and nothing was deployed.
