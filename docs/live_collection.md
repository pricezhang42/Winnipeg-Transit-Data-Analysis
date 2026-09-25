# Collecting live BLUE observations

The collector reads `WINNIPEG_TRANSIT_API_KEY` from the environment or the project's `.env`. The key is excluded from version-control patterns and sanitized from saved responses and errors. No additional Python packages are required.

Run commands from the project folder:

```bash
# Inspect the plan without network requests or database writes
.venv/bin/python -m src.collect --dry-run

# Capture one cycle, then exit
.venv/bin/python -m src.collect

# Collect continuously in this terminal; Ctrl+C stops it safely
.venv/bin/python -m src.collect --continuous

# Inspect stored capture counts, without contacting the API
.venv/bin/python -m src.collect --status
```

Continuous collection requires the computer and terminal process to stay running. No background service or scheduled task has been installed. A lock prevents two collectors from writing the same database concurrently. Completed requests survive interruption; restart the command to resume appending snapshots.

## Coverage and storage

`collector_config.json` selects BLUE and four starter stop keys: 11027, 10541, 61205, and 60066. Each cycle queries stop schedules from five minutes ago to thirty minutes ahead, discovers trip keys, then queries up to eight tracked trips. Tracking rotates through the least recently queried trips and expires them two hours after last discovery. This is a bounded sample, not complete route coverage. Stop IDs and vehicle IDs are preserved as identifiers, not inferred from historical row order.

Cycles start approximately sixty seconds apart, unless requests and retries take longer. All requests are paced at forty per minute, including retries. The API's documented limit is one hundred per IP/key per minute; other processes sharing the key/IP also count. HTTP 429 and server/transport failures have bounded retries and respect `Retry-After`; authentication rejection stops the run. Every attempt is stored, including errors, so missing captures are visible. [API overview](https://api.winnipegtransit.com/home/api/v4).

The local database is `data/live/transit.sqlite` (SQLite with WAL). Its tables are:

| Table | Purpose |
|---|---|
| `snapshots` | Append-only, timestamped response payloads, sanitized request parameters, status, payload hash, and provenance |
| `observations` | Each stop prediction in each response, including repeated revisions of the same event |
| `tracked_trips` | Restartable trip polling queue |
| `cycles` | Start, completion/error status, and counts per cycle |

Observation columns include trip, bus, stop, variant, scheduled and estimated departure times, cancellation status, collection time, and prediction lead time where the date is known. `fields_json` also preserves stop number separately from stop key, route key, variant name, arrival times, previous/next trip keys, schedule type, and time interpretation flags. A missing bus, cancellation field, or estimate stays missing. A response list index is only response order, not an official stop sequence.

Stop schedules returned full local date-times during live verification. Trip schedules returned clock times such as `19:16:00` with no date. Those clock times are retained as raw values and marked `date_missing`; absolute UTC times, delay, and lead time stay null for these entries. Resolving dates requires matching dated stop events to the same trip and scheduled-stop key, with explicit midnight and ambiguity checks. The effective-from/to dates describe the schedule's validity period and must not be substituted as the trip's operating date. Ambiguous or nonexistent local times at daylight-saving transitions are also flagged. [Trip schedule documentation](https://api.winnipegtransit.com/home/api/v4/services/trip-schedules).

## What this data can train

These are **API estimates**, not measured actual departures. Repeated snapshots retain what was known at each collection time. The database does not yet supply new ground-truth labels or feed the historical predictor automatically.

The next modeling step is to collect across new dates, resolve trip dates and event identity, and obtain later measured departure records. Historical records lack trip IDs, so matching by stop, destination, and scheduled time must detect collisions and reject ambiguous matches. Features must come from snapshots received before the prediction cutoff; later estimates must never leak into earlier predictions. Evaluate bus-specific live features on a newly reserved chronological test period after labels become available.

## Offline verification

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m src.collect --replay tests/fixtures/transit_synthetic.json \
  --database reports/collector_test/replay.sqlite
```

The fixture is explicitly invented. Replay requires a separate database and labels every response `synthetic_fixture`; it cannot count as a successful live capture. Live requests are labeled `live`. Replaying twice intentionally appends two sets of observations.

## Audit a timed capture

A timed test's `run.json` records its start/end times and database path. Reproduce the read-only audit with:

```bash
.venv/bin/python -m src.check_collection \
  --run-directory reports/collector_20min_20260915T002352Z
```

This writes `audit.json` and `report.md` beside the run metadata. The audit checks database integrity, response hashes, observation counts, payload normalization, polling gaps, repeated revisions, and candidate matches between dated stop events and clock-only trip events. It excludes earlier captures and does not claim prediction accuracy.
