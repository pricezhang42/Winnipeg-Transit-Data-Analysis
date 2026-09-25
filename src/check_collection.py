"""Audit a bounded collector run without changing its database or treating estimates as truth."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

from .transit_api import normalize


def audit_run(run_directory):
    folder = Path(run_directory).resolve()
    run = json.loads((folder / 'run.json').read_text())
    end = run.get('finished_at_utc', datetime.now(timezone.utc).isoformat())
    with sqlite3.connect(Path(run['database']).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        # One SQLite read transaction keeps the audit consistent if collection is still active.
        db.execute('BEGIN')
        integrity = [r[0] for r in db.execute('PRAGMA quick_check')]
        foreign_key_errors = [tuple(r) for r in db.execute('PRAGMA foreign_key_check')]
        snapshots = [dict(r) for r in db.execute(
            'SELECT * FROM snapshots WHERE request_started_at_utc >= ? AND request_started_at_utc <= ? ORDER BY received_at_utc',
            (run['started_at_utc'], end))]
        cycles = [dict(r) for r in db.execute(
            'SELECT * FROM cycles WHERE started_at_utc >= ? AND started_at_utc <= ? ORDER BY started_at_utc',
            (run['started_at_utc'], end))]
        observations = [dict(r) for r in db.execute('''SELECT o.*, s.resource_type FROM observations o
            JOIN snapshots s USING(snapshot_id) WHERE s.request_started_at_utc >= ? AND s.request_started_at_utc <= ?
            ORDER BY o.received_at_utc, o.snapshot_id, o.row_number''', (run['started_at_utc'], end))]
    by_snapshot = defaultdict(list)
    for row in observations:
        by_snapshot[row['snapshot_id']].append(row)
    mismatches = Counter()
    for snapshot in snapshots:
        saved = by_snapshot[snapshot['snapshot_id']]
        if len(saved) != snapshot['observation_count']:
            mismatches['snapshot_row_count'] += 1
        if hashlib.sha256(snapshot['payload_json'].encode()).hexdigest() != snapshot['payload_sha256']:
            mismatches['payload_hash'] += 1
        if snapshot['http_status'] == 200 and snapshot['error_kind'] is None:
            try:
                parsed = normalize(json.loads(snapshot['payload_json']), snapshot['resource_type'],
                                   snapshot['resource_id'], snapshot['received_at_utc'])
                saved = sorted(saved, key=lambda r:r['row_number'])
                if parsed != [json.loads(r['fields_json']) for r in saved]:
                    mismatches['normalized_payload'] += 1
            except (ValueError, TypeError, KeyError, AttributeError):
                mismatches['reparse_failure'] += 1

    fields = [json.loads(r['fields_json']) for r in observations]
    groups = defaultdict(list)
    intervals = defaultdict(list)
    for snapshot in snapshots:
        if snapshot['resource_type'] == 'stop' and snapshot['attempt_number'] == 1:
            intervals[snapshot['resource_id']].append(datetime.fromisoformat(snapshot['request_started_at_utc']).timestamp())
    for row, field in zip(observations, fields):
        if row['trip_key'] and row['scheduled_stop_key']:
            # Separate resource types: stop responses carry dates, trip responses may carry only a clock time.
            groups[(row['resource_type'], row['trip_key'], row['scheduled_stop_key'], field['scheduled_departure_raw'])].append(field)
    repeated = {key:values for key,values in groups.items() if len(values) > 1}
    changed = {key:values for key,values in repeated.items()
               if len({v['estimated_departure_raw'] for v in values if v['estimated_departure_raw'] is not None}) > 1}
    dated_stop = [r for r in observations if r['resource_type'] == 'stop' and r['estimated_delay_seconds'] is not None]
    future = [r for r in dated_stop if r['scheduled_lead_seconds'] is not None and r['scheduled_lead_seconds'] > 0]
    dated_events = {(r['trip_key'], r['scheduled_stop_key']) for r in observations
                    if r['resource_type'] == 'stop' and r['scheduled_departure_utc']}
    trip_events = {(r['trip_key'], r['scheduled_stop_key']) for r in observations if r['resource_type'] == 'trip'}
    matching_events = dated_events & trip_events
    checks_passed = integrity == ['ok'] and not foreign_key_errors and not mismatches
    result = {
        'run':run, 'audit_at_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'Only snapshots requested during this bounded run; excludes earlier captures.',
        'cycles':dict(Counter(r['status'] for r in cycles)), 'responses':len(snapshots),
        'http_statuses':dict(Counter(str(r['http_status']) for r in snapshots)),
        'errors':dict(Counter(r['error_kind'] for r in snapshots if r['error_kind'])),
        'retry_attempts':sum((r['attempt_number'] or 1) > 1 for r in snapshots),
        'origins':dict(Counter(r['origin'] for r in snapshots)), 'observations':len(observations),
        'observations_by_resource':dict(Counter(r['resource_type'] for r in observations)),
        'distinct_trips':len({r['trip_key'] for r in observations if r['trip_key']}),
        'distinct_buses':len({r['bus_key'] for r in observations if r['bus_key']}),
        'distinct_stops':len({r['stop_key'] for r in observations if r['stop_key']}),
        'missing_bus_observations':sum(r['bus_key'] is None for r in observations),
        'cancelled_observations':sum(r['cancelled'] == 1 for r in observations),
        'repeated_event_groups_by_resource':dict(Counter(key[0] for key in repeated)),
        'changed_estimate_groups_by_resource':dict(Counter(key[0] for key in changed)),
        'dated_stop_estimates':len(dated_stop), 'dated_stop_estimates_before_scheduled_departure':len(future),
        'clock_only_scheduled_times':sum(f['time_issues'].get('scheduled') == 'date_missing' for f in fields),
        'trip_date_matching_candidates':{'trip_endpoint_distinct_trips':len({t for t,e in trip_events}),
                                        'trips_with_dated_event_match':len({t for t,e in matching_events}),
                                        'exact_event_matches':len(matching_events),
                                        'note':'Identity matches only; dates have not been propagated to other stops.'},
        'time_issues':dict(Counter(f'{part}:{reason}' for f in fields for part,reason in f['time_issues'].items())),
        'stop_polling':{},
        'checks':{'passed':checks_passed,'sqlite_quick_check':integrity,'foreign_key_violations':len(foreign_key_errors),
                  'reconciliation_mismatches':dict(mismatches)},
        'interpretation':'Repeated API estimates are not independent trips or actual measured departure outcomes. This is a collector smoke test, not an accuracy evaluation.'}
    for stop, times in sorted(intervals.items()):
        gaps = [b-a for a,b in zip(times,times[1:])]
        result['stop_polling'][stop] = {'polls':len(times), 'median_gap_seconds':round(statistics.median(gaps),3) if gaps else None,
                                     'max_gap_seconds':round(max(gaps),3) if gaps else None}
    examples=[]
    for key, values in changed.items():
        if key[0] != 'stop':
            continue
        examples.append({'trip_key':key[1], 'scheduled_stop_key':key[2], 'scheduled_departure_raw':key[3],
                         'first_estimated_departure_raw':values[0]['estimated_departure_raw'],
                         'last_estimated_departure_raw':values[-1]['estimated_departure_raw'], 'snapshots':len(values)})
        if len(examples) == 5:
            break
    result['examples_of_revised_estimates'] = examples
    (folder / 'audit.json').write_text(json.dumps(result, indent=2)+'\n')
    lines = ['# Twenty-minute collector test', '',
             f"Run status: **{run['status']}**. Requested duration: {run['requested_duration_seconds']} seconds. "
             f"Measured duration: {run.get('elapsed_seconds', 'still running')} seconds.", '',
             f"Start: {run['started_at_utc']}. End: {run.get('finished_at_utc', 'pending')}.", '',
             '## Capture results', '', '| Measure | Result |', '|---|---:|',
             f"| Completed cycles | {result['cycles'].get('complete', 0)} |",
             f"| Responses | {len(snapshots)} |", f"| Errors | {sum(result['errors'].values())} |",
             f"| Retry attempts | {result['retry_attempts']} |", f"| Observation snapshots | {len(observations):,} |",
             f"| Distinct trip IDs | {result['distinct_trips']} |", f"| Distinct bus IDs | {result['distinct_buses']} |",
             f"| Distinct stop IDs | {result['distinct_stops']} |", '',
             'Counts cover this run only. Repeated snapshots of the same event are intentionally retained. '
             'Stop coverage through queried trips does not imply uniform or complete route coverage.', '',
             '## Polling consistency', '', '| Seed stop | Polls | Median gap | Maximum gap |', '|---|---:|---:|---:|']
    for stop, stats in result['stop_polling'].items():
        lines.append(f"| {stop} | {stats['polls']} | {stats['median_gap_seconds']} s | {stats['max_gap_seconds']} s |")
    lines += ['', '## Short test', '',
              f"Database integrity, stored observation counts, response hashes, and full payload re-normalization: **{'PASS' if checks_passed else 'FAIL'}**.", '',
              f"There were **{len(dated_stop):,} dated stop estimates**, of which **{len(future):,}** were captured before scheduled departure. "
              f"**{result['changed_estimate_groups_by_resource'].get('stop',0)}** repeatedly observed dated stop-event groups changed their estimate during collection.", '',
              f"**{result['clock_only_scheduled_times']:,}** trip observations have clock-only scheduled times. Raw times remain preserved with `date_missing`; "
              'they are not silently assigned a date. Resolving these dates and matching measured outcomes remain the next dataset work.', '',
              f"As a preliminary identity check, **{len({t for t,e in matching_events})} of {len({t for t,e in trip_events})}** trips queried at the trip endpoint "
              'also have an exact trip-ID and scheduled-stop-ID match in a dated stop snapshot from this run. '
              'This supplies candidate anchors for date resolution, not verified outcome labels.', '',
              'No MAE is reported: API estimates are not actual departures. A final API estimate is also not a verified outcome. '
              'Twenty minutes checks collection and storage behavior, not all-day reliability or prediction accuracy.', '',
              'Full counts and sample revised predictions are in [audit.json](audit.json). '
              'The run metadata and process log are in [run.json](run.json) and [collector.log](collector.log).', '']
    (folder / 'report.md').write_text('\n'.join(lines))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-directory', required=True)
    args = parser.parse_args()
    result = audit_run(args.run_directory)
    print(json.dumps({key:result[key] for key in ['cycles','responses','errors','observations','distinct_trips','distinct_buses','distinct_stops','checks']},indent=2))


if __name__ == '__main__':
    main()
