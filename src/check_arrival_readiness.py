"""Audit dated arrival forecasts without substituting departure outcomes for arrival truth."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3

from .transit_api import LOCAL_ZONE,api_time


def write_json(path,data):
    Path(path).write_text(json.dumps(data,indent=2,allow_nan=False)+'\n')


def select_arrival(rows,scheduled,horizon,max_age_seconds=90):
    cutoff=scheduled-timedelta(minutes=horizon)
    candidates=[r for r in rows if datetime.fromisoformat(r['received_at_utc'])<=cutoff]
    if not candidates:return None,'no_snapshot_before_cutoff'
    row=max(candidates,key=lambda r:(r['received_at_utc'],r['snapshot_id']))
    if (cutoff-datetime.fromisoformat(row['received_at_utc'])).total_seconds()>max_age_seconds:
        return None,'snapshot_stale'
    if row.get('cancelled') is True:return None,'cancelled'
    estimate,issue=api_time(row.get('estimated_arrival_raw'))
    if issue:return None,'arrival_estimate_missing_or_ambiguous'
    return {**row,'arrival_prediction_utc':estimate.isoformat(),'cutoff_utc':cutoff.isoformat(),
            'horizon_minutes':horizon,'arrival_predicted_delay_seconds':(estimate-scheduled).total_seconds()},None


def audit(database,output_directory):
    folder=Path(output_directory);folder.mkdir(parents=True,exist_ok=True)
    source=json.loads((folder/'source_metadata.json').read_text())
    coverage=json.loads((folder/'source_coverage.json').read_text())
    outcome_dates={row['date'][:10] for row in coverage}
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        snapshots=[dict(r) for r in db.execute("SELECT snapshot_id,resource_type,received_at_utc,http_status,error_kind FROM snapshots WHERE origin='live' ORDER BY received_at_utc")]
        observations=[]
        for row in db.execute('''SELECT o.fields_json,o.snapshot_id,o.received_at_utc FROM observations o
            JOIN snapshots s USING(snapshot_id) WHERE s.origin='live' AND s.resource_type='stop' ORDER BY o.received_at_utc'''):
            observations.append({**json.loads(row['fields_json']),'snapshot_id':row['snapshot_id'],'received_at_utc':row['received_at_utc']})
    by_day=defaultdict(list);captures_by_day=defaultdict(list);unresolved=0
    for row in observations:
        scheduled,issue=api_time(row.get('scheduled_arrival_raw'))
        if issue:unresolved+=1;continue
        row['scheduled_arrival_utc']=scheduled.isoformat()
        by_day[str(scheduled.astimezone(LOCAL_ZONE).date())].append(row)
    for row in snapshots:
        day=str(datetime.fromisoformat(row['received_at_utc']).astimezone(LOCAL_ZONE).date())
        captures_by_day[day].append(row)
    result={'checked_at_utc':datetime.now(timezone.utc).isoformat(),
            'latest_capture_in_read_transaction':snapshots[-1]['received_at_utc'] if snapshots else None,
            'target':'arrival','measured_arrival_outcomes_available':False,
            'reason':'City performance deviation is measured at departure; API arrival fields are estimates, not observed arrivals.',
            'source_rows_updated_utc':source['rows_updated_utc'],'published_departure_dates':sorted(outcome_dates),
            'scope':'Consistent read-only SQLite transaction; growing collector database left unchanged.',
            'dated_stop_snapshots':len(observations),'unresolved_scheduled_arrival_snapshots':unresolved,
            'capture_days':{},'arrival_event_days':{}}
    for day,rows in captures_by_day.items():
        result['capture_days'][day]={'responses':len(rows),'first_received_at_utc':rows[0]['received_at_utc'],
                                     'last_received_at_utc':rows[-1]['received_at_utc'],
                                     'errors':dict(Counter(r['error_kind'] for r in rows if r['error_kind'])),
                                     'http_statuses':dict(Counter(str(r['http_status']) for r in rows))}
    candidates=[]
    for day,rows in sorted(by_day.items()):
        events=defaultdict(list)
        for r in rows:events[(r['trip_key'],r['scheduled_stop_key'],r['scheduled_arrival_raw'])].append(r)
        gaps=[];invalid_estimates=0
        for row in rows:
            arrival,ai=api_time(row.get('estimated_arrival_raw'));departure,di=api_time(row.get('estimated_departure_raw'))
            if not ai and not di:gaps.append((departure-arrival).total_seconds())
            if ai:invalid_estimates+=1
        stats={'snapshots':len(rows),'distinct_scheduled_arrival_events':len(events),
               'distinct_trip_ids':len({r['trip_key'] for r in rows}),'distinct_stop_numbers':len({r['stop_number'] for r in rows}),
               'departure_outcomes_published_for_date':day in outcome_dates,
               'missing_or_unparseable_arrival_estimates':invalid_estimates,
               'arrival_departure_comparable_snapshots':len(gaps),
               'arrival_and_departure_estimates_differ':sum(g!=0 for g in gaps),
               'negative_estimated_dwell_snapshots':sum(g<0 for g in gaps),
               'maximum_estimated_dwell_seconds':max(gaps) if gaps else None,'horizons':{}}
        for horizon in [1,2,5,10,20]:
            chosen=[];exclusions=Counter()
            for event,history in events.items():
                scheduled=datetime.fromisoformat(history[0]['scheduled_arrival_utc'])
                row,reason=select_arrival(history,scheduled,horizon)
                if reason:exclusions[reason]+=1;continue
                chosen.append(row)
                candidates.append({key:row.get(key) for key in ['snapshot_id','received_at_utc','trip_key','scheduled_stop_key','stop_number',
                    'bus_key','variant_name','scheduled_arrival_utc','arrival_prediction_utc','arrival_predicted_delay_seconds',
                    'cutoff_utc','horizon_minutes']})
            stats['horizons'][str(horizon)]={'forecast_candidates':len(chosen),'distinct_trip_ids':len({r['trip_key'] for r in chosen}),
                                           'exclusions':dict(exclusions)}
        result['arrival_event_days'][day]=stats
    write_json(folder/'arrival_candidates.json',candidates)
    write_json(folder/'readiness.json',result)
    write_json(folder/'capture_manifest.json',{'snapshot_ids':[r['snapshot_id'] for r in snapshots],
                'latest_received_at_utc':result['latest_capture_in_read_transaction']})
    lines=['# Arrival prediction readiness with the expanded capture','',
           '**Arrival MAE cannot yet be measured.** The public City outcomes measure departures; '
           'the saved arrival timestamps are forecasts. Substituting a departure or the last API estimate would not produce an arrival accuracy score.','',
           f"Read-only capture audit: {result['checked_at_utc']}. Latest included capture: {result['latest_capture_in_read_transaction']}.",
           f"City outcome dataset last updated: {source['rows_updated_utc']}. Published departure dates checked: {', '.join(sorted(outcome_dates))}.",'',
           '## Capture coverage','', '| Local date | Responses | Errors | Dated arrival snapshots | Distinct scheduled arrivals | Trip IDs |', '|---|---:|---:|---:|---:|---:|']
    for day,stats in sorted(result['arrival_event_days'].items()):
        cap=result['capture_days'].get(day,{})
        lines.append(f"| {day} | {cap.get('responses',0)} | {sum(cap.get('errors',{}).values())} | {stats['snapshots']} | {stats['distinct_scheduled_arrival_events']} | {stats['distinct_trip_ids']} |")
    lines+=['','Response counts are grouped by local receipt date; event counts by scheduled arrival date. '
            'There are collection gaps, so elapsed span does not represent continuous collection. Repeated updates are preserved.','',
            '## Forecasts available at fixed cutoffs','',
            'These are forecast candidates, not evaluated outcomes. At each scheduled-arrival-minus-horizon cutoff, '
            'select the last snapshot received by that moment, no more than 90 seconds old. Cancelled or invalid estimates are excluded.','',
            '| Scheduled arrival date | 1 minute | 2 minutes | 5 minutes | 10 minutes | 20 minutes |','|---|---:|---:|---:|---:|---:|']
    for day,stats in sorted(result['arrival_event_days'].items()):
        lines.append('| '+day+' | '+' | '.join(str(stats['horizons'][str(h)]['forecast_candidates']) for h in [1,2,5,10,20])+' |')
    lines+=['','## Arrival versus departure','']
    for day,stats in sorted(result['arrival_event_days'].items()):
        lines.append(f"- {day}: arrival and departure estimates differ in {stats['arrival_and_departure_estimates_differ']} of "
                     f"{stats['arrival_departure_comparable_snapshots']} comparable snapshots. Maximum estimated difference: {stats['maximum_estimated_dwell_seconds']} seconds.")
    lines+=['','Even equal API arrival/departure estimates do not prove equal actual times. Actual time spent at the stop is unknown.','',
            '## Next valid evaluation','',
            'The larger departure evaluation can run when the City publishes outcomes for the new dates, using the previously fixed API, '
            'delay-persistence, revision and combined rules on matched cohorts. No parameters should be tuned against those outcomes before that comparison. '
            'True arrival evaluation requires an independent measured arrival feed or verified stop-arrival observations.','',
            'No MAE, arrival accuracy, or new model improvement is claimed by this report. The collector was not stopped or reconfigured.','',
            '[City measured-departure dataset](https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u/about_data). '
            '[API definitions of estimated arrival and departure](https://api.winnipegtransit.com/home/api/v4/services/stop-schedules).','']
    (folder/'report.md').write_text('\n'.join(lines))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database',default='data/live/transit.sqlite');p.add_argument('--output-directory',required=True)
    a=p.parse_args();r=audit(a.database,a.output_directory)
    print(json.dumps({k:r[k] for k in ['latest_capture_in_read_transaction','published_departure_dates','arrival_event_days']},indent=2))
