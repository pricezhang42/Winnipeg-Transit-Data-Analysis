# BLUE Trip Observatory

A static, credential-free dashboard backed by the local WTDA collector export. It displays single bus/trip histories, estimate replay, and conservatively matched City departure outcomes. Arrival mode never substitutes departure outcomes for actual arrivals.

## Refresh data

From the parent WTDA directory:

```bash
.venv/bin/python -m src.export_dashboard --refresh-outcomes
```

This reads the collector database without altering it. It writes public API-derived snapshots into `dashboard/dist/data`; no API credentials are exported. Re-publish the validated Site to update the hosted snapshot. The hosted page does not access the local SQLite database, and Reload data only retrieves the most recently published export.

For a local view:

```bash
.venv/bin/python -m http.server 8787 --bind 127.0.0.1 --directory dashboard/dist
```

## Validation

JavaScript syntax and all static asset paths were checked. Export validation covers trip identity, stop alignment, chronological snapshots, measured-match provenance, and credential exclusion. No browser UI QA was requested. The optional feature-detected WebMCP list/select tools are present; a supported WebMCP validation context was unavailable, so those tools are not claimed as verified. Normal page controls do not depend on WebMCP.

## Historical departures

`history.html` presents all 403,511 prepared BLUE records from July 25–September 12, 2026. Compact daily files are loaded only for the selected date range; records are filtered locally and displayed in pages of 100. All 710 flagged records are included by default. Date, stop, destination, quality, and sort controls operate on measured departure records; no historical bus or trip identity is inferred.

From the parent WTDA folder, regenerate the history assets with:

```bash
.venv/bin/python -m src.export_history
```

The aggregation module was checked against the source Parquet for record count, median delay, ±2-minute proportion and flagged-row count. Additional checks cover filter behavior, date rollover, missing delays, distribution boundaries and ordering. Browser UI QA was not requested. The optional historical WebMCP filter tool is feature-detected but has not been validated in a supported browser context.

## September 16 refresh

The published snapshot now includes 192 captured trips, with 5,070 matched measured-stop outcomes (4,638 on September 15). The history archive retains its original prepared rows and appends freshly retrieved City records for September 13–15, for 435,631 total BLUE records. Original training data and fitted models are unchanged. Supplemental source URLs and retrieval times are stored in the history index.

`src.export_history` now incorporates nonempty cached City dates newer than the prepared file, using the same parsing and quality flags. `src.export_dashboard --refresh-outcomes` refreshes the City cache for captured trip dates. The source remains a published snapshot, not an automatic live feed.
