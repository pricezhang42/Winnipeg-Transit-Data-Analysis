# Winnipeg Transit Observatory

A standalone static website: HTML, CSS, JavaScript modules, and exported JSON. No Codex tools, ChatGPT account, API key, npm install, build step, or external chart library is required to view it. Existing trip and historical snapshots and the pass-up export are included in `dist`.

## Run locally

From the repository root, with Python 3:

```bash
python3 -m http.server 8787 --bind 127.0.0.1 --directory dashboard/dist
```

Open these pages in any modern browser:

- [Bus trips and replay](http://localhost:8787/): captured BLUE trips and conservatively matched measured departures.
- [Historical departures](http://localhost:8787/history.html): measured BLUE stop departures with filters and quality flags.
- [Pass-ups](http://localhost:8787/passups.html): all-route reports, monthly/route/hour/type/weekday charts, filters, search, and paginated source records.

Use HTTP rather than double-clicking the HTML files: browser module and fetch security rules restrict `file://` pages. Stop the server with Ctrl+C.

## Host elsewhere

Copy the **contents of `dashboard/dist`** to any static HTTP host. Keep `assets`, `data`, `history`, and `passups` alongside the HTML files. All runtime assets and data use relative URLs, including when hosted under a subdirectory. Serve `.js` and `.mjs` as JavaScript and `.json` as JSON; no application server or platform-specific configuration is needed. Deploy the full export together to keep indexes and data chunks consistent. The website displays snapshots; Reload data does not connect to the collector.

## Refresh pass-up data

The exporter uses only the Python standard library:

```bash
python3 -m src.export_passups --source Transit_Pass-ups_20260925.csv
```

Use `--source` for a later download and optionally `--output` for another export directory. The included snapshot contains 199,249 reports from December 1, 2009 through September 24, 2026. Monthly chunks are fetched only for the selected date range (latest 12 calendar months initially). Unknown routes, missing coordinates, and repeated IDs remain in the dataset. Invalid timestamps stop export with a source record number rather than silently discarding data. The index records source filename, SHA-256, export time, dictionaries, counts, and column layout.

Counts represent reported events, not passengers affected or rates per rider/trip. No reporting coverage or service denominator is available. Partial periods and historical network changes limit comparisons. Times retain the source's Winnipeg local wall-clock values. The original CSV remains the source of record, including route names and raw location strings; the dashboard retains IDs and one-based source record numbers for traceability.

## Refresh existing views

These exporters use the analysis environment described in the root README:

```bash
.venv/bin/python -m src.export_dashboard --refresh-outcomes
.venv/bin/python -m src.export_history
```

Trip export requires the local collector SQLite database; historical export requires `data/processed/blue_clean.parquet` and optionally cached newer City dates. Neither is needed to view the bundled snapshots. The historical snapshot is separate from the new pass-up export and is not automatically replaced by newer raw performance CSV downloads. Copy refreshed output to your static host to update its snapshot.

## Validate

From the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_export_passups.py' -v
node --test tests/test_passups.mjs
node --check dashboard/dist/assets/passups.js
```

Tests cover record preservation, unknown routes, duplicates, coordinates, invalid timestamps, inclusive filters, midnight boundaries, aggregation totals, empty selections, and year rollover. Node is needed only for JavaScript development checks, not to serve or view the website.
