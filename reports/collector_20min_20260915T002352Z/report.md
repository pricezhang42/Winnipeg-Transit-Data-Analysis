# Twenty-minute collector test

Run status: **duration_complete**. Requested duration: 1200 seconds. Measured duration: 1200.01 seconds.

Start: 2026-09-15T00:23:52.308955+00:00. End: 2026-09-15T00:43:52.319240+00:00.

## Capture results

| Measure | Result |
|---|---:|
| Completed cycles | 20 |
| Responses | 240 |
| Errors | 0 |
| Retry attempts | 0 |
| Observation snapshots | 7,956 |
| Distinct trip IDs | 24 |
| Distinct bus IDs | 21 |
| Distinct stop IDs | 111 |

Counts cover this run only. Repeated snapshots of the same event are intentionally retained. Stop coverage through queried trips does not imply uniform or complete route coverage.

## Polling consistency

| Seed stop | Polls | Median gap | Maximum gap |
|---|---:|---:|---:|
| 10541 | 20 | 60.0 s | 60.004 s |
| 11027 | 20 | 60.0 s | 60.004 s |
| 60066 | 20 | 60.0 s | 60.004 s |
| 61205 | 20 | 60.0 s | 60.004 s |

## Short test

Database integrity, stored observation counts, response hashes, and full payload re-normalization: **PASS**.

There were **267 dated stop estimates**, of which **229** were captured before scheduled departure. **12** repeatedly observed dated stop-event groups changed their estimate during collection.

**7,689** trip observations have clock-only scheduled times. Raw times remain preserved with `date_missing`; they are not silently assigned a date. Resolving these dates and matching measured outcomes remain the next dataset work.

As a preliminary identity check, **15 of 24** trips queried at the trip endpoint also have an exact trip-ID and scheduled-stop-ID match in a dated stop snapshot from this run. This supplies candidate anchors for date resolution, not verified outcome labels.

No MAE is reported: API estimates are not actual departures. A final API estimate is also not a verified outcome. Twenty minutes checks collection and storage behavior, not all-day reliability or prediction accuracy.

Full counts and sample revised predictions are in [audit.json](audit.json). The run metadata and process log are in [run.json](run.json) and [collector.log](collector.log).
