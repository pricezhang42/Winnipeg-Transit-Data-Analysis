# Arrival prediction readiness with the expanded capture

**Arrival MAE cannot yet be measured.** The public City outcomes measure departures; the saved arrival timestamps are forecasts. Substituting a departure or the last API estimate would not produce an arrival accuracy score.

Read-only capture audit: 2026-09-16T01:00:18.530095+00:00. Latest included capture: 2026-09-16T01:00:00.661203+00:00.
City outcome dataset last updated: 2026-09-15T10:57:44+00:00. Published departure dates checked: 2026-09-14.

## Capture coverage

| Local date | Responses | Errors | Dated arrival snapshots | Distinct scheduled arrivals | Trip IDs |
|---|---:|---:|---:|---:|---:|
| 2026-09-14 | 264 | 0 | 309 | 37 | 24 |
| 2026-09-15 | 3324 | 0 | 6413 | 223 | 139 |

Response counts are grouped by local receipt date; event counts by scheduled arrival date. There are collection gaps, so elapsed span does not represent continuous collection. Repeated updates are preserved.

## Forecasts available at fixed cutoffs

These are forecast candidates, not evaluated outcomes. At each scheduled-arrival-minus-horizon cutoff, select the last snapshot received by that moment, no more than 90 seconds old. Cancelled or invalid estimates are excluded.

| Scheduled arrival date | 1 minute | 2 minutes | 5 minutes | 10 minutes | 20 minutes |
|---|---:|---:|---:|---:|---:|
| 2026-09-14 | 11 | 13 | 7 | 12 | 8 |
| 2026-09-15 | 175 | 176 | 177 | 182 | 186 |

## Arrival versus departure

- 2026-09-14: arrival and departure estimates differ in 0 of 309 comparable snapshots. Maximum estimated difference: 0.0 seconds.
- 2026-09-15: arrival and departure estimates differ in 0 of 6413 comparable snapshots. Maximum estimated difference: 0.0 seconds.

Even equal API arrival/departure estimates do not prove equal actual times. Actual time spent at the stop is unknown.

## Next valid evaluation

The larger departure evaluation can run when the City publishes outcomes for the new dates, using the previously fixed API, delay-persistence, revision and combined rules on matched cohorts. No parameters should be tuned against those outcomes before that comparison. True arrival evaluation requires an independent measured arrival feed or verified stop-arrival observations.

No MAE, arrival accuracy, or new model improvement is claimed by this report. The collector was not stopped or reconfigured.

[City measured-departure dataset](https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u/about_data). [API definitions of estimated arrival and departure](https://api.winnipegtransit.com/home/api/v4/services/stop-schedules).
