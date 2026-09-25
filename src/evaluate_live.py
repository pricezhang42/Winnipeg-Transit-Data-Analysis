"""Match dated live stop estimates to later measured departures and score fixed horizons."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import unicodedata

from .baseline import metrics
from .transit_api import api_time


def destination_key(value):
    # Normalize presentation only, never infer a different destination or route variant.
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def match_key(row, live=False):
    time, issue = api_time(row.get('scheduled_departure_raw') if live else row.get('scheduled_time'))
    stop = row.get('stop_number')
    destination = row.get('variant_name') if live else row.get('route_destination')
    if issue or not stop or not destination:
        return None
    return (str(row.get('route_number')), str(stop), destination_key(destination), time.isoformat())


def match_events(live_rows, outcomes):
    events = defaultdict(list)
    for row in live_rows:
        events[(row['trip_key'],row['scheduled_stop_key'],row['scheduled_departure_raw'])].append(row)
    actuals = defaultdict(list)
    live_identities = defaultdict(set)
    for row in outcomes:
        key = match_key(row)
        if key:
            actuals[key].append(row)
    for event, rows in events.items():
        key = match_key(rows[0], live=True)
        if key:
            live_identities[key].add(event)
    result=[]
    for event, rows in events.items():
        key = match_key(rows[0], live=True)
        candidates = actuals.get(key, [])
        reason = ('invalid_live_identity' if key is None else
                  'multiple_live_events_same_match_key' if len(live_identities[key]) > 1 else
                  'no_exact_outcome' if len(candidates) == 0 else
                  'multiple_outcomes' if len(candidates) > 1 else 'matched')
        result.append({'event':event,'rows':rows,'status':reason,'outcome':candidates[0] if reason=='matched' else None})
    return result


def select_at_horizon(rows, scheduled, horizon_minutes, max_age_seconds=90):
    cutoff = scheduled - timedelta(minutes=horizon_minutes)
    # Select what was last known at cutoff, then assess availability; never fall back to an older valid estimate.
    available = [row for row in rows if datetime.fromisoformat(row['received_at_utc']) <= cutoff]
    if not available:
        return None, 'no_snapshot_before_cutoff'
    selected = max(available, key=lambda row:(row['received_at_utc'],row['snapshot_id']))
    age = (cutoff - datetime.fromisoformat(selected['received_at_utc'])).total_seconds()
    if age > max_age_seconds:
        return None, 'snapshot_stale'
    if selected.get('cancelled') is True:
        return None, 'cancelled_at_cutoff'
    if selected.get('estimated_departure_utc') is None or selected.get('estimated_delay_seconds') is None:
        return None, 'estimate_missing'
    return selected, None


def evaluate(run_directory, outcome_path, output_directory):
    folder=Path(output_directory);folder.mkdir(parents=True,exist_ok=True)
    run=json.loads((Path(run_directory)/'run.json').read_text())
    protocol={'horizons_minutes':[5,10,20], 'max_snapshot_age_seconds':90,
              'matching':'Exact route, stop number, destination (case/whitespace normalization only), scheduled timestamp; require unique outcome and live event.',
              'selection':'Latest received snapshot at or before scheduled departure minus horizon; no pooling repeated predictions.',
              'prediction':'Previously captured Winnipeg Transit API departure estimate; no new model fitted.',
              'baseline':'Scheduled departure / zero delay, scored on identical event rows.',
              'actual_delay':'Negative of City deviation seconds.',
              'eligibility':'Exclude events already measured as departed by the forecast cutoff; report exclusions.',
              'date_policy':'Use full dated stop responses only. Clock-only trip responses excluded.'}
    (folder/'protocol.json').write_text(json.dumps(protocol,indent=2)+'\n')
    outcomes=json.loads(Path(outcome_path).read_text())
    with sqlite3.connect(Path(run['database']).resolve().as_uri()+'?mode=ro',uri=True) as db:
        rows=[]
        for raw,received,snapshot in db.execute('''SELECT o.fields_json,o.received_at_utc,o.snapshot_id FROM observations o
            JOIN snapshots s USING(snapshot_id) WHERE s.origin='live' AND s.resource_type='stop'
            AND s.request_started_at_utc>=? AND s.request_started_at_utc<=? ORDER BY o.received_at_utc''',
            (run['started_at_utc'],run['finished_at_utc'])):
            rows.append({**json.loads(raw),'received_at_utc':received,'snapshot_id':snapshot})
    matched=match_events(rows,outcomes)
    ledger=[{'trip_key':m['event'][0],'scheduled_stop_key':m['event'][1],'scheduled_departure_raw':m['event'][2],
             'stop_number':m['rows'][0]['stop_number'],'destination':m['rows'][0]['variant_name'],
             'snapshots':len(m['rows']),'status':m['status'],'outcome_row_id':m['outcome']['row_id'] if m['outcome'] else None} for m in matched]
    scored=[];scores={}
    for horizon in protocol['horizons_minutes']:
        exclusions=Counter(); selected_rows=[]
        for m in matched:
            if m['status']!='matched':
                exclusions[m['status']]+=1;continue
            source=m['outcome'];scheduled,_=api_time(source['scheduled_time'])
            delay=-float(source['deviation'])
            actual=scheduled+timedelta(seconds=delay)
            cutoff=scheduled-timedelta(minutes=horizon)
            chosen,reason=select_at_horizon(m['rows'],scheduled,horizon,protocol['max_snapshot_age_seconds'])
            if reason:
                exclusions[reason]+=1;continue
            if actual<=cutoff:
                exclusions['already_departed_at_cutoff']+=1;continue
            selected_rows.append({'horizon_minutes':horizon,'trip_key':m['event'][0],'scheduled_stop_key':m['event'][1],
                'outcome_row_id':source['row_id'],'stop_number':source['stop_number'],'destination':source['route_destination'],
                'snapshot_id':chosen['snapshot_id'],'received_at_utc':chosen['received_at_utc'],
                'cutoff_utc':cutoff.isoformat(),'scheduled_departure_utc':scheduled.isoformat(),'actual_departure_utc':actual.isoformat(),
                'actual_delay_seconds':delay,'api_predicted_delay_seconds':chosen['estimated_delay_seconds'],
                'api_absolute_error_seconds':abs(chosen['estimated_delay_seconds']-delay),
                'on_time_absolute_error_seconds':abs(delay),
                'snapshot_age_at_cutoff_seconds':(cutoff-datetime.fromisoformat(chosen['received_at_utc'])).total_seconds(),
                'flag_extreme_actual_delay':abs(delay)>3600})
        scored.extend(selected_rows)
        scores[str(horizon)]={'api':metrics([r['actual_delay_seconds'] for r in selected_rows],[r['api_predicted_delay_seconds'] for r in selected_rows]),
                              'on_time':metrics([r['actual_delay_seconds'] for r in selected_rows],[0]*len(selected_rows)),
                              'distinct_trips':len({r['trip_key'] for r in selected_rows}), 'exclusions':dict(exclusions)}
    result={'run':run,'live_stop_snapshots':len(rows),'distinct_live_departure_events':len(matched),'downloaded_outcomes':len(outcomes),
            'match_counts':dict(Counter(m['status'] for m in matched)),'scores':scores,
            'scored_distinct_departures':len({(r['trip_key'],r['scheduled_stop_key']) for r in scored}),
            'source_sha256':hashlib.sha256(Path(outcome_path).read_bytes()).hexdigest()}
    for name,value in [('matches',ledger),('predictions',scored),('metrics',result)]:
        (folder/(name+'.json')).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
    lines=['# Initial live prediction evaluation — September 14, 2026','',
           'This evaluates **the Winnipeg Transit API estimates captured during the twenty-minute test**, against later measured City departure records. No live-feature model has been trained.', '',
           f"Matched **{result['match_counts'].get('matched',0)} of {len(matched)}** distinct dated departure events. "
           f"The capture contains {len(rows)} dated stop snapshots; repeated snapshots are not treated as independent departures.", '',
           '| Minutes before scheduled departure | Departures | Trips | API MAE | On-time MAE | API within ±2 min |',
           '|---|---:|---:|---:|---:|---:|']
    for horizon,s in scores.items():
        a,b=s['api'],s['on_time']
        if a['n']:
            lines.append(f"| {horizon} | {a['n']} | {s['distinct_trips']} | {a['mae_seconds']:.1f} s | {b['mae_seconds']:.1f} s | {a['within_120_seconds']:.1%} |")
        else: lines.append(f"| {horizon} | 0 | 0 | — | — | — |")
    lines+=['','For each horizon, the latest snapshot received by the cutoff is used, with a maximum age of 90 seconds. '
            'Cancelled or missing estimates and already-departed events are excluded. The on-time comparator uses exactly the same departures. '
            'Each horizon can have a different cohort, so differences across horizons are not evidence that error improves with lead time.','',
            '## Matching and limitations','',
            'Matching requires exact route, stop number, scheduled date/time and destination, with only case/whitespace normalization. '
            'Ambiguous outcomes or multiple live events sharing a match key are excluded. The City records have no trip ID, so this is a conservative record linkage, not an independently verified trip-ID join.','',
            'Clock-only trip snapshots are not evaluated here. Unmatched departures are preserved in the match ledger; they are not assigned zero delay. '
            'Publication coverage may be incomplete, and missing outcomes are not necessarily random. The City warns that measured records can contain missing data and GPS errors.','',
            f"Only **{result['scored_distinct_departures']} distinct departures** contribute across the scored horizons. "
            'This single-evening sample is an initial accuracy check, not evidence of typical route-wide performance. Some departures share a trip and some appear at several horizons. '
            'The historical model has not been compared on this sample, so these numbers do not establish an improvement over it.','',
            '## Reproduction','',
            '```bash',f'.venv/bin/python -m src.evaluate_live --run-directory {run_directory} \\',
            f'  --outcomes {outcome_path} --output-directory {output_directory}','```','',
            '[Measured City dataset](https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u/about_data). '
            'The exact source query and retrieval time are saved in `source.json`; row-level matching and scoring are in `matches.json` and `predictions.json`.','']
    unchanged_horizons=[h for h in scores if scores[h]['api']['n'] and
                        all(r['api_predicted_delay_seconds']==0 for r in scored if str(r['horizon_minutes'])==h)]
    if unchanged_horizons:
        lines += ['All selected API estimates at '+', '.join(unchanged_horizons)+' minutes ahead equal the scheduled departure. '
                  'Their scores therefore match the on-time baseline exactly; these rows do not demonstrate a benefit from live updates.','']
    (folder/'report.md').write_text('\n'.join(lines))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-directory',required=True);p.add_argument('--outcomes',required=True);p.add_argument('--output-directory',required=True)
    a=p.parse_args();print(json.dumps(evaluate(a.run_directory,a.outcomes,a.output_directory),indent=2))
