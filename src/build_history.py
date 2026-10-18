"""Build departure-timing and pass-up-risk artifacts from post-overhaul history.

Pipeline (each stage is a function below):

  1. Import      Load every monthly ZIP and recent CSV of departure records into
                 one DuckDB table (`raw`). Files are streamed, never edited.
  2. Clean       Drop rows from before the June 2025 network overhaul, invalid rows,
                 exact duplicates across overlapping files, conflicting records and
                 service-day mismatches. What remains is one row per bus visit to a
                 stop (`visits`). Counts of everything dropped go into the audit.
  3. Coverage    Write `source-report.json`: input file hashes, the audit and how
                 many visits each date has.
  4. Pass-ups    Match reports to the nearest date-specific route stop and group
                 by report time, under three spatial-only tolerance profiles.
  5. Summaries   For each route, compute departure timing and report-count evidence.
                 Candidate labels are reviewed separately, never published as policy.
  6. Export      Write timing artifacts, versioned report evidence and policy review.
                 Report evidence is not a visit-based pass-up-risk artifact.

A "group" is route + boarding stop + destination (direction) + weekday/weekend +
two-hour window. All seasons are pooled. Group keys are JSON arrays such as
["BLUE","10638","university of manitoba","weekend",16], where 16 means 16:00-17:59.

Stages 1-3 are slow (tens of millions of rows) and can be skipped with
--skip-import once a working database exists; stages 4-6 always re-run.
"""
import argparse
import csv
import hashlib
import json
import math
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import duckdb

from export_reliability import destination, digest, distance, point
from report_policy_review import summarize_candidates

# --- Settings ---------------------------------------------------------------

OVERHAUL_START = '2025-06-29'          # Winnipeg's new network launched this day
START = OVERHAUL_START                 # older name, kept for callers
GROUPING = 'route-stop-destination-daytype-2hour-v1'
WINDOW_HOURS = 2
WINDOW_MINUTES = WINDOW_HOURS * 60

# Deviation is stored as delay in seconds: positive = late, negative = early.
MAX_VALID_DELAY = 7 * 24 * 3600        # beyond a week the record is broken, not late
TIMING_MAX_DELAY = 3600                # timing stats ignore visits more than 1 h off
EARLY_BELOW = -60                      # "early": left more than 1 minute early
LATE_ABOVE = 300                       # "late": left more than 5 minutes late

# Evidence needed before a group gets timing statistics.
MIN_OBSERVATIONS = 30
MIN_DATES = 5
Z_95 = 1.96                            # normal quantile for 95% share intervals

# Match reports spatially, then group by their own Winnipeg local timestamps.
# Profiles test spatial sensitivity only; there is no visit or time-gap matching.
STOP_MAX_METRES = 150
PROFILES = [('60m', 60), ('100m', 100), ('150m', 150)]
MATCHING_METHOD = 'nearest-stop-report-time-v1'
TIMING_REPORT_PROFILE = '100m'         # profile behind nearbyFullBusReports
REPORT_EVIDENCE_VERSION = 'stop-report-counts-v1'

INTERVAL_METHOD = '95% normal approximation with daily cluster-robust variance; descriptive, not forecast coverage'

# --- Small helpers -----------------------------------------------------------

def bin_hour(hour):
    """Start hour of the two-hour window containing `hour`: 16 and 17 both give 16."""
    return hour - hour % WINDOW_HOURS

def key(route, stop, dest, dt):
    """Group key for a scheduled time, in the same JSON form the backend looks up."""
    daytype = 'weekend' if dt.weekday() >= 5 else 'weekday'
    return json.dumps([route, str(stop), dest, daytype, bin_hour(dt.hour)], separators=(',', ':'))

def write(path, value):
    """Write compact JSON, creating folders as needed."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, separators=(',', ':'), default=str, allow_nan=False) + '\n')

def connect(work):
    """Open the working database with a bounded memory budget."""
    db = duckdb.connect(str(work / 'history.duckdb'))
    db.execute("SET memory_limit='1GB'; SET threads=2; SET preserve_insertion_order=false")
    return db

def count(db, sql, params=()):
    return db.execute(sql, params).fetchone()[0]

# Winnipeg records "Weekday", "Saturday" or "Sunday"; DuckDB's dayofweek is 0 = Sunday.
EXPECTED_SERVICE_DAY = "CASE WHEN dayofweek(scheduled)=6 THEN 'Saturday' WHEN dayofweek(scheduled)=0 THEN 'Sunday' ELSE 'Weekday' END"

# --- Stage 1: import ------------------------------------------------------------

POINT = r"'POINT\s*\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)'"
# Normalise one source CSV into raw's columns. Both timestamp formats are accepted:
# ISO in the monthly archives, "2026 Sep 24 04:35:00 PM" in the recent CSV. The
# source "Deviation" is positive when early, so delay is its negative.
IMPORT_SQL = f'''
INSERT INTO raw SELECT
  "Row ID",
  trim("Route Number"),
  trim("Stop Number"),
  regexp_replace(lower(trim("Route Destination")), '^to\\s+', ''),
  coalesce(try_cast("Scheduled Time" AS TIMESTAMP), try_strptime("Scheduled Time", '%Y %b %d %I:%M:%S %p')),
  -try_cast(replace("Deviation", ',', '') AS BIGINT),
  "Day Type",
  try_cast(regexp_extract("Location", {POINT}, 1) AS DOUBLE),
  try_cast(regexp_extract("Location", {POINT}, 2) AS DOUBLE)
FROM read_csv(?, header=true, all_varchar=true)'''

def import_sources(db, files, work, resume):
    """Stage 1: load every departure file into `raw`; return the input manifest.

    ZIPs hold one CSV each and are extracted one at a time to `current.csv`, so
    disk use stays at one month. With resume=True nothing is loaded; the manifest
    must match the one saved by the completed import.
    """
    if not resume:
        db.execute('CREATE OR REPLACE TABLE raw(id VARCHAR, route VARCHAR, stop VARCHAR, dest VARCHAR, '
                   'scheduled TIMESTAMP, delay BIGINT, service VARCHAR, lon DOUBLE, lat DOUBLE)')
    manifest = []
    for path in map(Path, files):
        print('Importing ' + path.name, flush=True)
        manifest.append({'file': path.name, 'sha256': digest(path), 'bytes': path.stat().st_size})
        if resume:
            continue
        if path.suffix.lower() == '.zip':
            with zipfile.ZipFile(path) as archive:
                members = [n for n in archive.namelist() if n.lower().endswith('.csv')]
                if len(members) != 1:
                    raise ValueError('Expected one CSV in ' + str(path))
                extracted = work / 'current.csv'
                with archive.open(members[0]) as src, extracted.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
            db.execute(IMPORT_SQL, [str(extracted)])
            extracted.unlink()
        else:
            db.execute(IMPORT_SQL, [str(path)])
        db.execute('CHECKPOINT')
    if resume:
        if json.loads((work / 'import-complete.json').read_text()) != manifest:
            raise ValueError('Resume input files do not match the completed import')
    else:
        write(work / 'import-complete.json', manifest)
    return manifest

# --- Stage 2: clean and deduplicate ------------------------------------------------

def clean_visits(db):
    """Stage 2: turn `raw` into `visits`, one row per bus visit; return the audit counts.

    Steps, each counted in the audit:
      unique_rows   post-overhaul, structurally valid rows; exact copies from
                    overlapping files collapse to one (SELECT DISTINCT)
      conflict_ids  source row IDs that appear with different contents: all dropped
      clean         remaining rows whose recorded service day matches the calendar
      identities    rows describing the same visit (route, stop, destination,
                    scheduled time) collapse; if their delays disagree, the visit
                    is dropped as a conflict
      visits        the kept visits, with derived columns used later
    """
    valid = (f"delay IS NOT NULL AND abs(delay) <= {MAX_VALID_DELAY} AND coalesce(id,'')<>'' "
             "AND coalesce(route,'')<>'' AND coalesce(stop,'')<>'' AND coalesce(dest,'')<>''")
    audit = {'sourceRows': count(db, 'SELECT count(*) FROM raw')}
    audit['beforeOverhaulRows'] = count(db, 'SELECT count(*) FROM raw WHERE scheduled < ?::TIMESTAMP', [OVERHAUL_START])
    audit['invalidRows'] = count(db, f'SELECT count(*) FROM raw WHERE scheduled IS NULL OR NOT ({valid})')

    db.execute(f'''CREATE OR REPLACE TABLE unique_rows AS
      SELECT DISTINCT id, route, stop, dest, scheduled, delay, service, lon, lat FROM raw
      WHERE scheduled >= ?::TIMESTAMP AND {valid}''', [OVERHAUL_START])
    audit['distinctSourceRows'] = count(db, 'SELECT count(*) FROM unique_rows')

    db.execute('''CREATE OR REPLACE TABLE conflict_ids AS
      SELECT id FROM unique_rows GROUP BY id
      HAVING count(DISTINCT (route, stop, dest, scheduled, delay, service)) > 1''')
    audit['conflictingSourceIds'] = count(db, 'SELECT count(*) FROM conflict_ids')

    db.execute(f'''CREATE OR REPLACE TABLE clean AS
      SELECT * FROM unique_rows ANTI JOIN conflict_ids USING (id)
      WHERE service = {EXPECTED_SERVICE_DAY}''')
    audit['calendarMismatchRows'] = count(db, f'SELECT count(*) FROM unique_rows WHERE service IS DISTINCT FROM {EXPECTED_SERVICE_DAY}')

    db.execute('''CREATE OR REPLACE TABLE identities AS
      SELECT route, stop, dest, scheduled,
             min(delay) AS delay, min(delay) <> max(delay) AS is_conflict,
             avg(lon) AS lon, avg(lat) AS lat, count(*) AS copies
      FROM clean GROUP BY route, stop, dest, scheduled''')
    audit['conflictingVisitIdentities'] = count(db, 'SELECT count(*) FROM identities WHERE is_conflict')
    audit['collapsedConsistentCopies'] = count(db, 'SELECT coalesce(sum(copies - 1), 0) FROM identities WHERE NOT is_conflict')

    # vid: visit id used by pass-up matching. actual/actual_day: when the bus really
    # left, which is what a pass-up report's timestamp is compared with.
    # hour: start of the two-hour window. g: the group key (see module docstring).
    db.execute(f'''CREATE OR REPLACE TABLE visits AS
      SELECT *, cast(json_array(route, stop, dest, daytype, hour) AS VARCHAR) AS g FROM (
        SELECT row_number() OVER () AS vid, route, stop, dest, scheduled, delay, lon, lat,
               scheduled + delay * INTERVAL 1 SECOND AS actual,
               cast(scheduled AS DATE) AS day,
               cast(scheduled + delay * INTERVAL 1 SECOND AS DATE) AS actual_day,
               CASE WHEN dayofweek(scheduled) IN (0, 6) THEN 'weekend' ELSE 'weekday' END AS daytype,
               cast(hour(scheduled) - hour(scheduled) % {WINDOW_HOURS} AS INTEGER) AS hour
        FROM identities WHERE NOT is_conflict)''')
    audit['retainedRecordedVisits'] = count(db, 'SELECT count(*) FROM visits')
    for name in ['raw', 'unique_rows', 'conflict_ids', 'clean', 'identities']:
        db.execute('DROP TABLE ' + name)
    return audit

# --- Stage 3: coverage report ------------------------------------------------------

def write_source_report(db, manifest, audit, work):
    """Stage 3: save inputs, audit and per-date coverage to `source-report.json`.

    The departure "sha256" is a fingerprint of the whole input bundle (every file
    hash plus the cutoff); the backend requires both manifests to share it.
    """
    lo, hi = db.execute('SELECT min(day), max(day) FROM visits').fetchone()
    coverage = db.execute('SELECT day, count(*) FROM visits GROUP BY day ORDER BY day').fetchall()
    bundle = hashlib.sha256(json.dumps({'files': manifest, 'cutoff': OVERHAUL_START}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    meta = {'schemaVersion': 2, 'coverageStart': str(lo), 'coverageEnd': str(hi), 'overhaulCutoff': OVERHAUL_START, 'audit': audit,
            'sources': {'departures': {'sha256': bundle, 'files': manifest}},
            'dailyCoverage': [{'date': str(d), 'visits': n} for d, n in coverage]}
    write(work / 'source-report.json', meta)

def ingest(files, work, resume=False):
    """Stages 1-3: build the working database of visits from the departure files."""
    with connect(work) as db:
        manifest = import_sources(db, files, work, resume)
        audit = clean_visits(db)
        write_source_report(db, manifest, audit, work)
        db.execute('CHECKPOINT')
    print('Imported ' + str(audit['retainedRecordedVisits']) + ' deduplicated visits', flush=True)

# --- Stage 4: pass-up report matching ---------------------------------------------------

def read_reports(passups, covered_dates, audit):
    """Full-bus pass-up reports on dates that have departure data, one per report ID."""
    reports, seen = [], set()
    with open(passups, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            audit['sourceReports'] += 1
            if row['Pass-Up Type'] != 'Full Bus Pass-Up':
                audit['nonFullBusReports'] += 1
                continue
            dt = datetime.strptime(row['Time'], '%m/%d/%Y %I:%M:%S %p')
            if dt.date().isoformat() not in covered_dates:
                audit['outsideCoveredDates'] += 1
                continue
            audit['reportsOnCoveredDates'] += 1
            if row['Pass-Up ID'] in seen:
                audit['duplicateReports'] += 1
                continue
            seen.add(row['Pass-Up ID'])
            coords = point(row['Location'])
            if not coords:
                audit['invalidCoordinates'] += 1
                continue
            reports.append((row['Pass-Up ID'], row['Route Number'], destination(row['Route Destination']), dt, dt.date(), *coords))
    return reports

def stops_on_report_days(db):
    """Average each stop's valid coordinates on the report's route/direction/date."""
    db.execute('''CREATE OR REPLACE TABLE day_stops AS
      SELECT v.route, v.dest, v.day, v.stop, avg(v.lon) lon, avg(v.lat) lat
      FROM grouped_visits v SEMI JOIN (SELECT DISTINCT route, dest, day FROM reports) r USING (route, dest, day)
      WHERE v.lon BETWEEN -180 AND 180 AND v.lat BETWEEN -90 AND 90
      GROUP BY v.route, v.dest, v.day, v.stop''')
    stops = defaultdict(dict)
    for route, dest, day, stop, lon, lat in db.execute('SELECT * FROM day_stops').fetchall():
        stops[(route, dest, day)][stop] = (lon, lat)
    return stops


def nearest_stop_candidates(reports, stops, audit):
    """Pick the nearest stop within STOP_MAX_METRES; stop ID breaks distance ties."""
    candidates = []
    for rid, route, dest, dt, day, lon, lat in reports:
        nearest = min(((distance((lon, lat), coords), stop)
                       for stop, coords in stops.get((route, dest, day), {}).items()), default=None)
        if nearest is None or nearest[0] > STOP_MAX_METRES:
            audit['noNearbyStop'] += 1
            continue
        meters, stop = nearest
        candidates.append((rid, route, dest, stop, dt, day, meters))
    return candidates


def match_reports(db, passups, meta, work):
    """Match a nearest stop and count unique report IDs by report time and date.

    Different IDs are different reports even if they may describe the same bus.
    Departure timestamps and visit IDs never determine report membership.
    """
    audit = Counter()
    reports = read_reports(passups, {r['date'] for r in meta['dailyCoverage']}, audit)
    db.execute('CREATE OR REPLACE TABLE reports(rid VARCHAR, route VARCHAR, dest VARCHAR, reported TIMESTAMP, day DATE, lon DOUBLE, lat DOUBLE)')
    if reports:
        db.executemany('INSERT INTO reports VALUES (?,?,?,?,?,?,?)', reports)
    print('Matching ' + str(len(reports)) + ' reports to date-specific route stops', flush=True)
    candidates = nearest_stop_candidates(reports, stops_on_report_days(db), audit)
    grouped = defaultdict(lambda: defaultdict(dict))
    match_rows = []
    for rid, route, dest, stop, reported, day, meters in candidates:
        group = key(route, stop, dest, reported)
        for profile, max_metres in PROFILES:
            if meters <= max_metres:
                grouped[group][profile][rid] = day.isoformat()
                audit[profile + 'Reports'] += 1
                match_rows.append({'report': rid, 'profile': profile, 'stop': stop, 'group': group,
                                   'reportedTime': reported.isoformat(), 'date': day.isoformat(),
                                   'distanceMeters': round(meters, 2)})
    evidence = {}
    for group, profiles in grouped.items():
        dates = list(profiles['150m'].values())
        evidence[group] = {
            'coverageStart': min(dates), 'coverageEnd': max(dates),
            'profiles': {name: {'reports': len(ids), 'reportDates': len(set(ids.values()))}
                         for name, ids in profiles.items()},
        }
    audit['matchedReports'] = len(candidates)
    write(work / 'matches.json', match_rows)
    write(work / 'passup-audit.json', dict(audit))
    return evidence, dict(audit)

# --- Stage 5: per-route summaries ------------------------------------------------------

def query_records(db, sql):
    """Read SQL results by column name instead of positional tuple offsets."""
    result = db.execute(sql)
    columns = [column[0] for column in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def share_intervals(shares, daily):
    """95% descriptive intervals, treating each date as an independent cluster.

    For share p, expand sum((daily_count - p * daily_visits)**2) using
    the daily sums of squares and cross-products. This avoids retaining raw
    departures in Python while accounting for shared traffic/weather within a day.
    """
    dates = daily['dates']
    visits = daily['visits']
    intervals = []
    for category, share in zip(('early', 'within', 'late'), shares):
        squared_residuals = (daily[category + '_squares']
                             - 2 * share * daily[category + '_cross']
                             + share * share * daily['visit_squares'])
        variance = dates / (dates - 1) * squared_residuals / (visits * visits)
        margin = Z_95 * math.sqrt(max(0, variance))
        intervals.append([round(max(0, share - margin), 4), round(min(1, share + margin), 4)])
    return intervals


def timing_groups(db, reports):
    """Summarize timing in three steps: daily counts, eligible groups, quantiles.

    All timing counts use the same filtered visits and threshold definitions.
    Departure exposure references are separate and retain very late recorded visits.
    """
    db.execute(f'''CREATE OR REPLACE TEMP TABLE daily AS
      SELECT g, day, count(*) AS visits,
             count(*) FILTER (WHERE delay < {EARLY_BELOW}) AS early,
             count(*) FILTER (WHERE delay BETWEEN {EARLY_BELOW} AND {LATE_ABOVE}) AS within_window,
             count(*) FILTER (WHERE delay > {LATE_ABOVE}) AS late,
             count(*) FILTER (WHERE delay < 0) AS before_schedule
      FROM route_visits WHERE abs(delay) <= {TIMING_MAX_DELAY}
      GROUP BY g, day''')
    summaries = query_records(db, f'''
      SELECT g, sum(visits) AS visits, count(*) AS dates,
             min(day) AS coverage_start, max(day) AS coverage_end,
             sum(early) AS early, sum(within_window) AS within_window,
             sum(late) AS late, sum(before_schedule) AS before_schedule,
             sum(visits*visits) AS visit_squares,
             sum(early*early) AS early_squares,
             sum(within_window*within_window) AS within_squares,
             sum(late*late) AS late_squares,
             sum(early*visits) AS early_cross,
             sum(within_window*visits) AS within_cross,
             sum(late*visits) AS late_cross
      FROM daily GROUP BY g
      HAVING visits >= {MIN_OBSERVATIONS} AND dates >= {MIN_DATES}''')
    if not summaries:
        return {}
    quantiles = dict(db.execute(f'''
      SELECT g, quantile_cont(delay, [0.1, 0.5, 0.9])
      FROM route_visits WHERE abs(delay) <= {TIMING_MAX_DELAY} GROUP BY g''').fetchall())
    timing = {}
    for summary in summaries:
        group = summary['g']
        visits = summary['visits']
        shares = [summary[name] / visits for name in ('early', 'within_window', 'late')]
        p10, median, p90 = quantiles[group]
        timing[group] = {
            'observations': visits, 'distinctDates': summary['dates'],
            'coverageStart': str(summary['coverage_start']), 'coverageEnd': str(summary['coverage_end']),
            'medianSeconds': median, 'p10Seconds': p10, 'p90Seconds': p90,
            'earlyShare': shares[0], 'withinShare': shares[1], 'lateShare': shares[2],
            'beforeScheduleShare': summary['before_schedule'] / visits,
            'shareIntervals': share_intervals(shares, summary),
            'nearbyFullBusReports': reports.get(group, {}).get('profiles', {}).get(TIMING_REPORT_PROFILE, {}).get('reports', 0),
        }
    return timing

def report_evidence(db, reports, route):
    """Keep report groups even when no scheduled departures share their window.

    Recorded departures are exposure context, not a denominator for a probability:
    report groups use reported times; reference visits use scheduled times.
    """
    references = {g: {'recordedDepartures': n, 'departureDates': dates,
                      'referenceCoverageStart': str(lo), 'referenceCoverageEnd': str(hi)}
                  for g, n, dates, lo, hi in db.execute(
                      'SELECT g, count(*), count(DISTINCT day), min(day), max(day) FROM route_visits GROUP BY g').fetchall()}
    groups = set(references) | {g for g in reports if json.loads(g)[0] == route}
    evidence = {}
    for group in sorted(groups):
        matched = reports.get(group, {})
        profiles = {name: matched.get('profiles', {}).get(name, {'reports': 0, 'reportDates': 0})
                    for name, _ in PROFILES}
        evidence[group] = {
            **references.get(group, {'recordedDepartures': 0, 'departureDates': 0,
                                     'referenceCoverageStart': None, 'referenceCoverageEnd': None}),
            'reportCount': profiles['150m']['reports'], 'reportDates': profiles['150m']['reportDates'],
            'coverageStart': matched.get('coverageStart'), 'coverageEnd': matched.get('coverageEnd'),
            'profiles': profiles,
        }
    return evidence

# --- Stage 6: export -------------------------------------------------------------------

def write_route_groups(out, folder, route, groups):
    """Write one route shard and return its relative path for the manifest."""
    relative_path = folder + '/' + quote(route, safe='') + '.json'
    write(out / relative_path, {'groups': groups})
    return relative_path


def export(db, meta, passups, out, work):
    """Stages 4-6: aggregate reports, summarize timing and write evidence/review files."""
    # Recompute group keys from the stored columns, so a database imported with an
    # older grouping (e.g. one-hour or seasonal keys) is still exported correctly.
    db.execute(f"CREATE OR REPLACE TEMP VIEW grouped_visits AS SELECT * EXCLUDE (g), "
               f"cast(json_array(route, stop, dest, daytype, hour - hour % {WINDOW_HOURS}) AS VARCHAR) g FROM visits")
    reports, report_audit = match_reports(db, passups, meta, work)
    meta['passupMatchingMethod'] = MATCHING_METHOD
    meta['sources']['passups'] = {'file': Path(passups).name, 'sha256': digest(passups)}

    routes = [r[0] for r in db.execute('SELECT DISTINCT route FROM visits ORDER BY route').fetchall()]
    timing_files, report_files, total_groups = {}, {}, 0
    review_groups = {}
    for route in routes:
        print('Summarizing ' + route, flush=True)
        db.execute('CREATE OR REPLACE TEMP TABLE route_visits AS SELECT * FROM grouped_visits WHERE route=?', [route])
        timing = timing_groups(db, reports)
        evidence = report_evidence(db, reports, route)
        review_groups.update(evidence)
        timing_files[route] = write_route_groups(out, 'reliability-routes', route, timing)
        report_files[route] = write_route_groups(out, 'passup-report-routes', route, evidence)
        total_groups += len(timing)

    # Manifests: the backend reads these, then loads one route file per ride on demand.
    common = {'schemaVersion': 2, 'grouping': GROUPING, 'groupWindowMinutes': WINDOW_MINUTES, 'passupMatchingMethod': MATCHING_METHOD, 'coverageStart': meta['coverageStart'],
              'coverageEnd': meta['coverageEnd'], 'sources': meta['sources'], 'routes': routes}
    timing = {**common, 'groups': {}, 'routeFiles': timing_files, 'eligibleGroups': total_groups, 'intervalMethod': INTERVAL_METHOD,
              'minObservations': MIN_OBSERVATIONS, 'minDates': MIN_DATES}
    report_counts = {**common, 'schemaVersion': 3, 'evidenceVersion': REPORT_EVIDENCE_VERSION,
                     'evidenceUnit': 'unique-report-id', 'groupTimeBasis': 'reported-local-time',
                     'referenceTimeBasis': 'scheduled-local-time', 'labelStatus': 'pending_policy_review',
                     'groups': {}, 'routeFiles': report_files, 'passupAudit': report_audit}
    review = summarize_candidates(review_groups)
    write(out / 'reliability.json', timing)
    write(out / 'passup-report-counts.json', report_counts)
    write(out / 'reliability.report.json', {**meta, 'eligibleGroups': total_groups, 'intervalMethod': INTERVAL_METHOD, 'passupAudit': report_audit})
    write(out / 'passup-report-counts.report.json', {k: v for k, v in report_counts.items() if k not in ['groups', 'routeFiles']})
    write(out / 'passup-policy-review.json', review)
    print(json.dumps({'coverage': [meta['coverageStart'], meta['coverageEnd']], 'routes': len(routes), 'timingGroups': total_groups,
                      'reportGroups': len(review_groups), 'passups': report_audit}), flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--departures', nargs='+', required=True, help='monthly ZIPs and/or recent CSVs')
    parser.add_argument('--passups', required=True, help='City pass-up CSV')
    parser.add_argument('--work', required=True, help='folder for the working DuckDB database (several GB)')
    parser.add_argument('--output', required=True, help='folder for the exported artifacts')
    parser.add_argument('--skip-import', action='store_true', help='reuse the existing database; skip stages 1-3')
    parser.add_argument('--resume-import', action='store_true', help='rows are already in `raw`; redo stages 2-3 only')
    args = parser.parse_args()
    work, out = Path(args.work), Path(args.output)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    if not args.skip_import:
        ingest(args.departures, work, args.resume_import)
    with connect(work) as db:
        meta = json.loads((work / 'source-report.json').read_text())
        export(db, meta, args.passups, out, work)

if __name__ == '__main__':
    main()
