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
  4. Pass-ups    Match City full-bus pass-up reports to the recorded visit they most
                 likely describe, under three distance/time tolerance profiles.
  5. Summaries   For each route, compute departure-timing statistics per group and a
                 Low/Medium/High pass-up label per group (see passup_policy.py).
  6. Export      Write per-route JSON files plus the two manifests the backend loads.

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
from passup_policy import POLICY, classify

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

# Pass-up report matching. A report must sit near one stop clearly closer than any
# other, and near one visit clearly closer in time than any other.
STOP_MAX_METRES = 150
STOP_MIN_GAP_METRES = 30               # nearest stop must beat the runner-up by this
UNSTABLE_STOP_METRES = 30              # a stop that moved more than this in a day is skipped
VISIT_MAX_SECONDS = 300
VISIT_MIN_GAP_SECONDS = 60             # nearest visit must beat the runner-up by this
# (name, max metres, max seconds): labels must agree under all three tolerances.
PROFILES = [('60m_120s', 60, 120), ('100m_180s', 100, 180), ('150m_300s', 150, 300)]
TIMING_REPORT_PROFILE = '100m_180s'    # profile behind nearbyFullBusReports

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
    db = connect(work)
    manifest = import_sources(db, files, work, resume)
    audit = clean_visits(db)
    write_source_report(db, manifest, audit, work)
    db.execute('CHECKPOINT')
    db.close()
    print('Imported ' + str(audit['retainedRecordedVisits']) + ' deduplicated visits', flush=True)

# --- Stage 4: pass-up report matching ---------------------------------------------------

def read_reports(passups, covered_dates, audit):
    """Full-bus pass-up reports on dates that have departure data, one per report ID."""
    reports, seen = [], set()
    with open(passups, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            audit['sourceReports'] += 1
            if row['Pass-Up Type'] != 'Full Bus Pass-Up':
                continue
            dt = datetime.strptime(row['Time'], '%m/%d/%Y %I:%M:%S %p')
            if dt.date().isoformat() not in covered_dates:
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
    """Where each stop of a route/direction was on each day that has a report.

    Uses that day's recorded coordinates, since stops move over a year. A stop whose
    coordinates spread more than UNSTABLE_STOP_METRES within the day is unstable.
    """
    db.execute('''CREATE OR REPLACE TABLE day_stops AS
      SELECT v.route, v.dest, v.day, v.stop, avg(v.lon) lon, avg(v.lat) lat,
             min(v.lon) lo_lon, max(v.lon) hi_lon, min(v.lat) lo_lat, max(v.lat) hi_lat
      FROM grouped_visits v SEMI JOIN (SELECT DISTINCT route, dest, day FROM reports) r USING (route, dest, day)
      WHERE v.lon BETWEEN -180 AND 180 AND v.lat BETWEEN -90 AND 90
      GROUP BY v.route, v.dest, v.day, v.stop''')
    stops, unstable = defaultdict(dict), set()
    for route, dest, day, stop, lon, lat, lo_lon, hi_lon, lo_lat, hi_lat in db.execute('SELECT * FROM day_stops').fetchall():
        stops[(route, dest, day)][stop] = (lon, lat)
        if distance((lo_lon, lo_lat), (hi_lon, hi_lat)) > UNSTABLE_STOP_METRES:
            unstable.add((route, dest, day, stop))
    return stops, unstable

def nearest_stop_candidates(reports, stops, unstable, audit):
    """Assign each report to its nearest stop when that stop is clearly the one meant."""
    candidates = []
    for rid, route, dest, dt, day, lon, lat in reports:
        ranked = sorted((distance((lon, lat), p), s) for s, p in stops[(route, dest, day)].items())[:2]
        clear = (ranked and ranked[0][0] <= STOP_MAX_METRES
                 and (len(ranked) == 1 or ranked[1][0] - ranked[0][0] >= STOP_MIN_GAP_METRES)
                 and (route, dest, day, ranked[0][1]) not in unstable)
        if clear:
            candidates.append((rid, route, dest, ranked[0][1], dt, day, ranked[0][0]))
        else:
            audit['noUnambiguousNearbyStop'] += 1
    return candidates

def nearest_visits(db, candidates):
    """For each report, the two recorded visits at its stop closest in actual departure time.

    Visits on the report day and the days either side are considered (a late bus can
    cross midnight), within VISIT_MAX_SECONDS of the report time.
    """
    db.execute('CREATE OR REPLACE TABLE candidates(rid VARCHAR, route VARCHAR, dest VARCHAR, stop VARCHAR, reported TIMESTAMP, day DATE, meters DOUBLE)')
    if candidates:
        db.executemany('INSERT INTO candidates VALUES (?,?,?,?,?,?,?)', candidates)
    db.execute(f'''CREATE OR REPLACE TABLE timed AS
      WITH report_days AS (
        SELECT *, cast(day + shift * INTERVAL 1 DAY AS DATE) AS join_day
        FROM candidates CROSS JOIN (VALUES (-1), (0), (1)) d(shift))
      SELECT c.rid, v.vid, v.g, v.day, abs(epoch(v.actual) - epoch(c.reported)) AS delta,
             row_number() OVER (PARTITION BY c.rid ORDER BY abs(epoch(v.actual) - epoch(c.reported)), v.vid) AS ranking
      FROM report_days c
      JOIN grouped_visits v ON c.route = v.route AND c.dest = v.dest AND c.stop = v.stop AND c.join_day = v.actual_day
      WHERE abs(epoch(v.actual) - epoch(c.reported)) <= {VISIT_MAX_SECONDS}
      QUALIFY ranking <= 2''')
    timed = defaultdict(list)
    for rid, vid, g, day, delta, ranking in db.execute('SELECT * FROM timed ORDER BY rid, ranking').fetchall():
        timed[rid].append((vid, g, day, delta))
    return timed

def match_reports(db, passups, meta, work):
    """Stage 4: count matched pass-up reports per group, per matching profile.

    Returns (profile_groups, audit): profile_groups[g][profile] = (matched visits,
    distinct dates). A report counts once, for its single clearly nearest visit;
    several reports on one visit count once. Unmatched reports are only audited
    (policy reported-pattern-v2). Writes matches.json and passup-audit.json to `work`.
    """
    audit = Counter()
    reports = read_reports(passups, {r['date'] for r in meta['dailyCoverage']}, audit)
    db.execute('CREATE OR REPLACE TABLE reports(rid VARCHAR, route VARCHAR, dest VARCHAR, reported TIMESTAMP, day DATE, lon DOUBLE, lat DOUBLE)')
    db.executemany('INSERT INTO reports VALUES (?,?,?,?,?,?,?)', reports)
    print('Matching ' + str(len(reports)) + ' reports with date-specific route stops', flush=True)
    stops, unstable = stops_on_report_days(db)
    candidates = nearest_stop_candidates(reports, stops, unstable, audit)
    timed = nearest_visits(db, candidates)

    accepted, match_rows = defaultdict(dict), []   # accepted[profile][visit id] = (group, date)
    for rid, route, dest, stop, dt, day, meters in candidates:
        visits = timed[rid]
        clear = visits and (len(visits) == 1 or visits[1][3] - visits[0][3] >= VISIT_MIN_GAP_SECONDS)
        if not clear:
            audit['noUnambiguousTimedVisit'] += 1
        for profile, max_metres, max_seconds in PROFILES:
            if clear and meters <= max_metres and visits[0][3] <= max_seconds:
                vid, g, visit_day, delta = visits[0]
                accepted[profile][vid] = (g, visit_day)
                audit[profile + 'Reports'] += 1
                match_rows.append({'report': rid, 'profile': profile, 'visit': vid, 'group': g, 'date': str(visit_day),
                                   'distanceMeters': round(meters, 2), 'timeSeconds': delta})

    profile_groups = defaultdict(dict)
    for profile, visits in accepted.items():
        matched, dates = Counter(), defaultdict(set)
        for g, day in visits.values():
            matched[g] += 1
            dates[g].add(day)
        for g, n in matched.items():
            profile_groups[g][profile] = (n, len(dates[g]))
        audit[profile + 'DistinctVisits'] = len(visits)
    write(work / 'matches.json', match_rows)
    write(work / 'passup-audit.json', dict(audit))
    return profile_groups, dict(audit)

# --- Stage 5: per-route summaries ------------------------------------------------------

def share_intervals(shares, n, moments):
    """95% intervals for the early/within/late shares, treating each date as a cluster.

    Same-day departures share traffic and weather, so they are not independent. For a
    share p = x / n built from daily counts (x_d of n_d), the cluster-robust variance is
        D/(D-1) * sum_d (x_d - p n_d)^2 / n^2
    and sum_d (x_d - p n_d)^2 = sum x_d^2 - 2p sum x_d n_d + p^2 sum n_d^2,
    which is why `moments` holds those daily sums.
    """
    days, nn, ee, ww, ll, en, wn, ln = moments
    intervals = []
    for p, xx, xn in zip(shares, [ee, ww, ll], [en, wn, ln]):
        se = math.sqrt(max(0, days / (days - 1) * (xx - 2 * p * xn + p * p * nn) / (n * n)))
        intervals.append([round(max(0, p - Z_95 * se), 4), round(min(1, p + Z_95 * se), 4)])
    return intervals

def timing_groups(db, profiles):
    """Departure-timing statistics for every group of the current route with enough evidence.

    Only visits within TIMING_MAX_DELAY of schedule count here. (Pass-up denominators
    keep very late visits: a late bus still visited the stop.)
    """
    early, within, late = f'delay < {EARLY_BELOW}', f'delay BETWEEN {EARLY_BELOW} AND {LATE_ABOVE}', f'delay > {LATE_ABOVE}'
    # Per group and date: visits and early/within/late counts, for the interval sums.
    db.execute(f'''CREATE OR REPLACE TEMP TABLE daily AS
      SELECT g, day, count(*) n, count(*) FILTER (WHERE {early}) e, count(*) FILTER (WHERE {within}) w, count(*) FILTER (WHERE {late}) l
      FROM route_visits WHERE abs(delay) <= {TIMING_MAX_DELAY} GROUP BY g, day''')
    moments = {row[0]: row[1:] for row in db.execute(
        'SELECT g, count(*), sum(n*n), sum(e*e), sum(w*w), sum(l*l), sum(e*n), sum(w*n), sum(l*n) FROM daily GROUP BY g').fetchall()}
    rows = db.execute(f'''
      SELECT g, count(*) n, count(DISTINCT day) d, min(day), max(day), quantile_cont(delay, [0.1, 0.5, 0.9]),
             count(*) FILTER (WHERE {early}), count(*) FILTER (WHERE {within}), count(*) FILTER (WHERE {late}), count(*) FILTER (WHERE delay < 0)
      FROM route_visits WHERE abs(delay) <= {TIMING_MAX_DELAY}
      GROUP BY g HAVING n >= {MIN_OBSERVATIONS} AND d >= {MIN_DATES}''').fetchall()
    timing = {}
    for g, n, d, lo, hi, (p10, median, p90), e, w, l, before in rows:
        shares = [e / n, w / n, l / n]
        timing[g] = {'observations': n, 'distinctDates': d, 'coverageStart': str(lo), 'coverageEnd': str(hi),
                     'medianSeconds': median, 'p10Seconds': p10, 'p90Seconds': p90,
                     'earlyShare': shares[0], 'withinShare': shares[1], 'lateShare': shares[2], 'beforeScheduleShare': before / n,
                     'shareIntervals': share_intervals(shares, n, moments[g]),
                     'nearbyFullBusReports': profiles[g].get(TIMING_REPORT_PROFILE, (0, 0))[0]}
    return timing

def risk_groups(db, profiles, labels, reasons):
    """Pass-up label for every group of the current route; only Low/Medium/High are kept.

    Every recorded visit counts in the denominator, however late. Label and reason
    tallies are added to `labels` and `reasons`.
    """
    risk = {}
    for g, n, days, lo, hi in db.execute('SELECT g, count(*), count(DISTINCT day), min(day), max(day) FROM route_visits GROUP BY g').fetchall():
        level, reason = classify(n, days, profiles[g])
        labels[level] += 1
        reasons[reason] += 1
        if level != 'unknown':
            risk[g] = {'level': level, 'recordedVisits': n, 'distinctDates': days, 'coverageStart': str(lo), 'coverageEnd': str(hi),
                       'profiles': {p: {'reportedVisits': profiles[g].get(p, (0, 0))[0], 'reportDates': profiles[g].get(p, (0, 0))[1]}
                                    for p in POLICY['profiles']}}
    return risk

# --- Stage 6: export -------------------------------------------------------------------

def export(db, meta, passups, out, work):
    """Stages 4-6: match reports, summarise each route and write all artifacts to `out`."""
    # Recompute group keys from the stored columns, so a database imported with an
    # older grouping (e.g. one-hour or seasonal keys) is still exported correctly.
    db.execute(f"CREATE OR REPLACE TEMP VIEW grouped_visits AS SELECT * EXCLUDE (g), "
               f"cast(json_array(route, stop, dest, daytype, hour - hour % {WINDOW_HOURS}) AS VARCHAR) g FROM visits")
    profiles, report_audit = match_reports(db, passups, meta, work)
    meta['sources']['passups'] = {'file': Path(passups).name, 'sha256': digest(passups)}

    routes = [r[0] for r in db.execute('SELECT DISTINCT route FROM visits ORDER BY route').fetchall()]
    timing_files, risk_files, labels, reasons, total_groups = {}, {}, Counter(), Counter(), 0
    for route in routes:
        print('Summarizing ' + route, flush=True)
        db.execute('CREATE OR REPLACE TEMP TABLE route_visits AS SELECT * FROM grouped_visits WHERE route=?', [route])
        timing = timing_groups(db, profiles)
        risk = risk_groups(db, profiles, labels, reasons)
        filename = quote(route, safe='') + '.json'
        timing_files[route] = 'reliability-routes/' + filename
        risk_files[route] = 'passup-risk-routes/' + filename
        write(out / timing_files[route], {'groups': timing})
        write(out / risk_files[route], {'groups': risk})
        total_groups += len(timing)

    # Manifests: the backend reads these, then loads one route file per ride on demand.
    common = {'schemaVersion': 2, 'grouping': GROUPING, 'groupWindowMinutes': WINDOW_MINUTES, 'coverageStart': meta['coverageStart'],
              'coverageEnd': meta['coverageEnd'], 'sources': meta['sources'], 'routes': routes}
    timing = {**common, 'groups': {}, 'routeFiles': timing_files, 'eligibleGroups': total_groups, 'intervalMethod': INTERVAL_METHOD,
              'minObservations': MIN_OBSERVATIONS, 'minDates': MIN_DATES}
    risk = {**common, 'groups': {}, 'routeFiles': risk_files, 'policy': POLICY, 'labelCounts': dict(labels), 'reasonCounts': dict(reasons)}
    write(out / 'reliability.json', timing)
    write(out / 'passup-risk.json', risk)
    write(out / 'reliability.report.json', {**meta, 'eligibleGroups': total_groups, 'intervalMethod': INTERVAL_METHOD, 'passupAudit': report_audit})
    write(out / 'passup-risk.report.json', {k: v for k, v in risk.items() if k not in ['groups', 'routeFiles']})
    print(json.dumps({'coverage': [meta['coverageStart'], meta['coverageEnd']], 'routes': len(routes), 'timingGroups': total_groups,
                      'labels': dict(labels), 'passups': report_audit}), flush=True)

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
    db = connect(work)
    export(db, json.loads((work / 'source-report.json').read_text()), args.passups, out, work)
    db.close()

if __name__ == '__main__':
    main()
