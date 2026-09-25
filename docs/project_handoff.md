# Project summary and agent handoff

Updated: **2026-09-25**. This is the starting point for another agent. Counts below are dated observations, not claims of an automatically updating service.

## Purpose and current result

Analyze Winnipeg Transit BLUE on-time performance, evaluate whether live estimates improve departure predictions, and let the user inspect a single bus throughout a trip. Work now includes a historical data pipeline, baseline and CatBoost experiments, a live API collector, exploratory live-signal evaluations, and a published static dashboard with trip replay and historical departures.

The core unresolved modeling question is whether estimate revisions and the same bus's earlier-stop delay provide useful information beyond the Transit API estimate. Small preliminary tests have mixed results; no live-feature model has been promoted. True arrival prediction accuracy remains unmeasurable with the available departure-only outcomes.

## Repository and local artifacts

- Workspace: `/home/linpu/workspace/WTDA`.
- GitHub: <https://github.com/pricezhang42/Winnipeg-Transit-Data-Analysis>, public, branch `main`.
- Initial root commit pushed September 25: `6293f1585678d2df6a89821addcd8005f50aebde`.
- `dashboard/` also has an independent nested Git repository used for Sites publishing. The root repository tracks its actual files, not a submodule. Preserve both repositories; root GitHub pushes and dashboard Sites deployments are separate operations. Check both working trees before publishing.
- `.gitignore` excludes all CSV files, `.env` secrets, `.venv`, processed Parquet, live SQLite data, dashboard outcome cache, generated prediction Parquet and CatBoost `.cbm` models. These files remain local; a fresh clone does not contain everything needed to reproduce analyses.
- The original CSV is `Recent_Transit_On-Time_Performance_Data_20260913.csv` (751,851,351 bytes). Do not replace it with a newer rolling download and claim the same experiment was reproduced. Configurations and manifests identify the source/splits.
- Credentials are provided locally through `.env` (`WINNIPEG_TRANSIT_API_KEY`). Never print, copy into documentation, commit, or export its value. `.env.example` is safe setup guidance.

## Data meaning and rules that must be preserved

The City's [recent on-time performance dataset](https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u/about_data) supplies measured **departure** deviations. Public JSON endpoint: `https://data.winnipeg.ca/resource/gp3k-am4u.json`; metadata endpoint: `https://data.winnipeg.ca/api/views/gp3k-am4u.json`.

- City deviation is negative for late departures. Our target is `delay_seconds = -deviation_seconds`: positive means late, negative means early. Measured departure = scheduled departure + our delay.
- Historical rows have stop, destination, schedule and deviation, but no bus ID, trip ID or reliable stop sequence. Never infer a bus's previous stop by shifting historical rows. Destination alone is not a route variant.
- The live API supplies trip/bus identity and predicted times. Estimates are not actual passage observations. A past estimated time does not establish that the bus passed that stop.
- Trip responses may contain clock-only times. Resolve dates against unambiguous dated stop events for the same trip/bus and handle midnight; reject ambiguity. Schedule validity dates are not operating dates.
- Dashboard measured outcomes are unique joins on stop, destination and scheduled departure. Identity comes from the live capture, not from the City outcome. Missing or ambiguous joins stay missing.
- All time interpretation uses `America/Winnipeg`; historical timestamps are local wall-clock times. Calendar date is not automatically a transit service date.
- For evaluation, features and date anchors must exist by the forecast cutoff. Later outcomes are labels only. Do not reuse the dashboard's retrospective display logic blindly as a forecasting feature pipeline.
- “Minutes ahead” in the existing departure evaluations means minutes before the scheduled departure, not before the eventual measured departure. Check each protocol before comparing tables.
- MAE is average absolute prediction error. Model “within ±2 min” is prediction error within 120 seconds; a historical dashboard on-time proportion instead measures deviation from schedule. They answer different questions.
- Retain quality flags and source provenance. Missing observations do not prove cancellations. Repeated snapshots/stops of one trip are not independent samples.

## Completed progress

### Historical preparation and modeling

The original prepared BLUE data has **403,511 rows** covering July 25–September 12, 2026, with no BLUE rows September 6–9. `src/data.py` preserves identifiers, raw values and quality flags. The prepared file is `data/processed/blue_clean.parquet`.

1. Historical median baseline: stop/destination/day type/hour groups, minimum 20 training observations, successively broader fallbacks ending at the training global median. Initial train July 25–August 16; validation August 17–23; test August 24–30. See `reports/first_baseline.md`.
2. CatBoost: train July 25–August 23; validation August 24–30; test August 31–September 5. Features are stop, destination, day type, weekday and scheduled minute of day. On 60,592 test departures, all-valid MAE was 144.18 s on-time, 135.50 s global median, 123.24 s grouped median, and **121.54 s CatBoost**. See `reports/regression/report.md`.
3. Small tuning experiment: four recipes over three chronological validation weeks. Longer training improved validation MAE 123.94 → 122.22 s, but September 10–12 holdout MAE worsened slightly, 202.14 → 202.77 s. The default model was not replaced. That holdout has now been inspected and cannot be called untouched. See `reports/tuning/report.md`.

### Live collection and evaluation

`src.collect` appends sanitized responses and normalized observations to `data/live/transit.sqlite`. Four seed stops are configured (11027, 10541, 61205, 60066), with up to eight trip polls per cycle, approximately 60-second cycles, and 40 requests/minute pacing. This is sampled coverage. A database lock prevents duplicate writers. No persistent service was installed by this work.

The first timed capture ran September 14 approximately 19:23–19:43 Winnipeg time. Run metadata and audit are in `reports/collector_20min_20260915T002352Z/`. Later collection extended coverage on September 15–16.

Read-only status checked **September 25**: **6,732 stored live responses, 222,596 observations**, no reported stored errors, latest receipt **2026-09-16T15:46:37.741579+00:00**. This does not prove a collector is currently running; check the actual process and recent timestamps before starting or reporting one.

- `reports/live_evaluation_20260914/`: tiny first measured-departure evaluation. Some longer-horizon API predictions equal schedule exactly, explaining equality with the on-time baseline.
- `reports/live_signals_20260914/`: exploratory fixed revision, earlier-stop persistence, blend and combined rules on **9 distinct departures across 6 trips**. Combined rule helped at some near horizons and hurt at others. Earlier-stop delay is estimated, not measured. No learned model or deployed rule resulted.
- `reports/live_evaluation_expanded/report.md`: larger capture readiness audit, written before September 15 outcomes were available. It does not report arrival MAE or a completed larger departure comparison.
- September 15 measured departures were subsequently downloaded and linked for the dashboard. **The larger fixed-rule evaluation was not completed merely by refreshing the dashboard.**

### Dashboard

Published URL: <https://winnipeg-blue-trip-observatory.wasdpyzlp.chatgpt.site>. Historical view: `/history.html`. Access was owner-private at the last deployment; verify current access before future publishing and preserve the audience requested by the user.

Latest completed deployment: September 16, version 3, dashboard source commit `c257590c517d9db82121963e040cb1e945a87552`.

- Trip export generated September 16 at 14:31 UTC: **192 trips**, **5,070 measured-stop links**, including **4,638 on September 15**. Nineteen snapshots were skipped for lacking an unambiguous dated anchor.
- Historical export: **435,631 BLUE rows**, July 25–September 15, 49 dates with records, 758 flagged rows. This combines the original 403,511 rows and cached September 13–15 City records. It does not alter the original modeling dataset or fitted models.
- Trip page: date/destination/bus-or-trip filters, estimate replay, arrival/departure modes, stop table and chart. Gold shows matched measured departures; blue shows captured API estimates. Arrival mode cannot show measured arrivals.
- History page: date/stop/destination/quality filtering, daily comparisons, distribution and paginated records. It cannot identify historical buses or trips.
- The hosted site is a **static published snapshot**, not connected to the collector. Reloading the page cannot import new local data.
- Verified at last refresh: JavaScript syntax, data alignment/counts, source-row identity, deviation sign, chronology, static asset availability and credential exclusion. Browser UI QA was not performed. Optional feature-detected WebMCP tools were not verified in a supported context.

Authoritative exported counts/provenance are in `dashboard/dist/data/index.json` and `dashboard/dist/history/index.json`. `reports/dashboard_release.json` still contains older counts (173 trips, 388 links). Earlier README history sections also describe only the original 403,511 rows. Do not confuse those older descriptions with the September 16 export or live database totals.

## Code map

| Area | Main files |
| --- | --- |
| Historical preparation/baseline | `src/data.py`, `src/baseline.py`, `src/pipeline.py`, `src/plots.py` |
| Historical regression/inference | `src/features.py`, `src/regression.py`, `src/predict.py`, `src/tune.py` |
| API/storage/collection | `src/transit_api.py`, `src/collect.py`, `src/check_collection.py` |
| Live evaluation | `src/evaluate_live.py`, `src/live_signals.py`, `src/test_live_signals.py`, `src/check_arrival_readiness.py` |
| Dashboard export | `src/export_dashboard.py`, `src/export_history.py` |
| Trip UI | `dashboard/dist/index.html`, `dashboard/dist/assets/app.js` |
| History UI | `dashboard/dist/history.html`, `dashboard/dist/assets/history.js`, `history-core.mjs` in the same assets directory |
| Tests | `tests/`, including synthetic collector fixtures |

## Commands and operating procedure

Run from the root workspace. Python 3.12 was verified; a local `.venv` already exists. For a fresh clone, create a venv, install `requirements.txt`, restore the exact raw CSV if reproducing historical experiments, and obtain local credentials separately for live collection.

```bash
# Offline checks; no need to rerun training for documentation-only changes
.venv/bin/python -m unittest discover -s tests -v

# Read stored counts / inspect collection plan
.venv/bin/python -m src.collect --status
.venv/bin/python -m src.collect --dry-run

# Start only after checking that another collector is not already running
.venv/bin/python -m src.collect --continuous

# Reproduce historical experiments (overwrites each experiment's outputs)
.venv/bin/python -m src.pipeline --config config.json
.venv/bin/python -m src.regression --config regression_config.json
.venv/bin/python -m src.tune --config tuning_config.json

# Refresh local dashboard data
.venv/bin/python -m src.export_dashboard --refresh-outcomes
.venv/bin/python -m src.export_history

# Serve the static dashboard locally
.venv/bin/python -m http.server 8787 --bind 127.0.0.1 --directory dashboard/dist
```

The trip exporter refreshes City outcomes only for captured trip dates. The history exporter reads the original prepared file plus newer dates already present in `data/dashboard_cache/`; it does not fetch every missing day itself. September 13 was fetched separately for the last refresh. A future refresh must deliberately fetch and validate any desired extra historical dates, with pagination/truncation checks and source URL/retrieval provenance. Published City rows can change between retrievals.

To reproduce the initial live experiments, use the commands in [live collection](live_collection.md), `reports/live_evaluation_20260914/report.md` and `reports/live_signals_20260914/report.md`. Evaluators require a `--run-directory`, `--outcomes` JSON and `--output-directory`. For an expanded run, inspect the expected run metadata and outcome schema in code; do not assume arbitrary cache JSON is accepted. Save new results separately rather than overwriting the original evidence.

To publish a changed dashboard, read the currently installed Sites building/hosting skills. Reuse `dashboard/.openai/hosting.json` and its project ID; do not create a replacement site. Validate exports, push the exact dashboard source state, package its static `dist` assets, save that version, deploy with the appropriate existing audience, and wait for successful deployment. A root GitHub push alone does not publish the site. Do not persist source push credentials in Git configuration or documentation.

## Recommended next work

1. Audit current collector process/storage and date coverage. Do not infer continuous collection from a long elapsed time range or from stored totals.
2. Expand the **departure** comparison using available September 15 and later labeled captures, preserving the previously fixed API, on-time, persistence, revision and blend rules. Report matched coverage and paired cohorts at each horizon, date and trip; prevent future information leakage.
3. Before tuning a learned live-feature model, reserve genuinely new chronological dates. Compare API-only and added-feature variants on identical cohorts; handle dependence across stops/trips/dates. Existing inspected dates are development evidence.
4. If actual arrival evaluation is requested, first obtain independent measured arrival observations. Do not relabel departure outcomes or final API forecasts as actual arrivals.
5. For dashboard maintenance, check new source publication and capture coverage, refresh both exports, validate and publish. Reconcile stale summary documentation/release metadata when doing that work. Automatic collection or scheduled publishing is not currently configured.

No collection, model training, dashboard refresh, publishing or new prediction evaluation was performed while writing this handoff. The September 25 check was read-only. This document records completed work and proposed next steps; it is not authorization to execute every proposed step automatically.
