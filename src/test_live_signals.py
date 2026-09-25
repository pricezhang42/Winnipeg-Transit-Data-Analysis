"""Exploratory fixed-rule ablation of live estimate revisions and earlier-stop estimates."""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from .baseline import metrics
from .evaluate_live import match_events,select_at_horizon
from .live_signals import revision_feature,upstream_feature
from .transit_api import api_time

PROTOCOL={
    'horizons_minutes':[1,2,5,10,20], 'current_max_age_seconds':90,
    'revision_lag_seconds':180,'revision_history_max_age_seconds':90,
    'trip_max_age_seconds':240,'earlier_stop_max_estimated_passage_age_seconds':600,
    'trend_rule':'API + revision rate * min(horizon, 3 minutes), adjustment clipped to +/-120 seconds.',
    'upstream_rule':'Carry forward the latest earlier-stop estimated delay on the same trip and bus.',
    'blend_rule':'Equal-weight average of current target API delay and earlier-stop estimated delay.',
    'combined_rule':'Equal-weight average of trend-adjusted API delay and earlier-stop estimated delay.',
    'date_policy':'Anchor trip clocks to the dated target stop snapshot known at cutoff; reject inconsistent sequence, ambiguous dates, >12h trips and >1h clock-estimate offsets.',
    'cohorts':'Separate matched availability cohorts for revision, upstream, and both; API baseline scored on identical rows.',
    'labels':'Later measured departures only for matching and scoring; never an earlier-stop feature.',
    'status':'Exploratory reuse of a previously inspected single-evening sample; rules fixed before this run, no parameter fitting or selection.'}


def write_json(path,value):
    Path(path).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')


def load_capture(run):
    stops=[];trips=[]
    with sqlite3.connect(Path(run['database']).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute('''SELECT s.* FROM snapshots s WHERE s.origin='live' AND
            s.request_started_at_utc>=? AND s.request_started_at_utc<=? ORDER BY s.received_at_utc''',
            (run['started_at_utc'],run['finished_at_utc'])):
            row=dict(row)
            if row['resource_type']=='trip':
                trips.append({**row,'payload':json.loads(row['payload_json'])})
            else:
                for raw, in db.execute('SELECT fields_json FROM observations WHERE snapshot_id=? ORDER BY row_number',(row['snapshot_id'],)):
                    stops.append({**json.loads(raw),'received_at_utc':row['received_at_utc'],'snapshot_id':row['snapshot_id']})
    return stops,trips


def build_rows(stops,trips,protocol):
    events=defaultdict(list)
    for row in stops:
        events[(row['trip_key'],row['scheduled_stop_key'],row['scheduled_departure_raw'])].append(row)
    rows=[];exclusions=Counter()
    for event,history in events.items():
        scheduled,issue=api_time(event[2])
        if issue:
            exclusions['date_missing']+=1;continue
        for horizon in protocol['horizons_minutes']:
            cutoff=scheduled-timedelta(minutes=horizon)
            current,reason=select_at_horizon(history,scheduled,horizon,protocol['current_max_age_seconds'])
            if reason:
                exclusions[f'{horizon}:{reason}']+=1;continue
            revision,revision_reason=revision_feature(history,current,cutoff,protocol['revision_lag_seconds'],protocol['revision_history_max_age_seconds'])
            upstream,upstream_reason=upstream_feature(trips,current,cutoff,protocol['trip_max_age_seconds'])
            api=current['estimated_delay_seconds']
            trend=api+max(-120,min(120,revision['revision_seconds_per_minute']*min(horizon,3))) if revision else None
            proxy=upstream['upstream_delay_seconds'] if upstream else None
            rows.append({'trip_key':event[0],'scheduled_stop_key':event[1],'scheduled_departure_raw':event[2],
                         'horizon_minutes':horizon,'cutoff_utc':cutoff.isoformat(),
                         'current_snapshot_id':current['snapshot_id'],'current_received_at_utc':current['received_at_utc'],
                         'current_bus_key':current['bus_key'],'api_prediction_seconds':api,
                         'revision_available':revision is not None,'revision_unavailable_reason':revision_reason,
                         'upstream_available':upstream is not None,'upstream_unavailable_reason':upstream_reason,
                         **(revision or {}),**(upstream or {}),
                         'trend_prediction_seconds':trend,'upstream_prediction_seconds':proxy,
                         'blend_prediction_seconds':(api+proxy)/2 if proxy is not None else None,
                         'combined_prediction_seconds':(trend+proxy)/2 if trend is not None and proxy is not None else None})
    return rows,dict(exclusions)


def run_experiment(run_directory,outcomes_path,output_directory):
    folder=Path(output_directory);folder.mkdir(parents=True,exist_ok=True)
    write_json(folder/'protocol.json',PROTOCOL)
    run=json.loads((Path(run_directory)/'run.json').read_text())
    stops,trips=load_capture(run)
    # Feature extraction is finished and saved before outcomes are even loaded.
    features,exclusions=build_rows(stops,trips,PROTOCOL)
    write_json(folder/'features.json',features)
    outcomes=json.loads(Path(outcomes_path).read_text())
    matches=match_events(stops,outcomes)
    by_event={tuple(m['event']):m for m in matches}
    scored=[];label_exclusions=Counter()
    for feature in features:
        event=(feature['trip_key'],feature['scheduled_stop_key'],feature['scheduled_departure_raw'])
        matched=by_event[event]
        if matched['status']!='matched':
            label_exclusions[matched['status']]+=1;continue
        source=matched['outcome'];scheduled,_=api_time(source['scheduled_time'])
        actual=-float(source['deviation']);cutoff=datetime.fromisoformat(feature['cutoff_utc'])
        if scheduled+timedelta(seconds=actual)<=cutoff:
            label_exclusions['already_departed_at_cutoff']+=1;continue
        scored.append({**feature,'outcome_row_id':source['row_id'],'actual_delay_seconds':actual,
                       'actual_departure_utc':(scheduled+timedelta(seconds=actual)).isoformat()})
    cohorts={
        'revision':(['trend'],lambda r:r['revision_available']),
        'upstream':(['upstream','blend'],lambda r:r['upstream_available']),
        'both':(['trend','upstream','blend','combined'],lambda r:r['revision_available'] and r['upstream_available'])}
    comparisons=[]
    for name,(candidates,eligible) in cohorts.items():
        for horizon in PROTOCOL['horizons_minutes']:
            subset=[r for r in scored if r['horizon_minutes']==horizon and eligible(r)]
            if not subset:continue
            predictions={'api':[r['api_prediction_seconds'] for r in subset],'on_time':[0]*len(subset)}
            predictions.update({candidate:[r[candidate+'_prediction_seconds'] for r in subset] for candidate in candidates})
            scores={name:metrics([r['actual_delay_seconds'] for r in subset],values) for name,values in predictions.items()}
            comparisons.append({'cohort':name,'horizon_minutes':horizon,'departures':len(subset),
                                'distinct_trips':len({r['trip_key'] for r in subset}),'scores':scores})
    result={'protocol':PROTOCOL,'input_hashes':{'outcomes_sha256':hashlib.sha256(Path(outcomes_path).read_bytes()).hexdigest(),
                                             'features_sha256':hashlib.sha256((folder/'features.json').read_bytes()).hexdigest()},
            'dated_live_departures':len(matches),'matched_departures':sum(m['status']=='matched' for m in matches),
            'feature_event_horizon_rows':len(features),'scored_event_horizon_rows':len(scored),
            'scored_distinct_departures':len({(r['trip_key'],r['scheduled_stop_key']) for r in scored}),
            'scored_distinct_trips':len({r['trip_key'] for r in scored}),
            'current_exclusions':exclusions,'label_exclusions':dict(label_exclusions),
            'revision_unavailable':dict(Counter(r['revision_unavailable_reason'] for r in scored if not r['revision_available'])),
            'upstream_unavailable':dict(Counter(r['upstream_unavailable_reason'] for r in scored if not r['upstream_available'])),
            'comparisons':comparisons}
    write_json(folder/'predictions.json',scored);write_json(folder/'metrics.json',result)
    lines=['# Do revisions and earlier-stop estimates add information?','',
           '**Exploratory test of fixed prediction rules on the previously inspected September 14 twenty-minute capture.** '
           'No model is fitted and no rule is selected for deployment. These are hypotheses tested on a very small reused sample.','',
           f"The scored sample has **{result['scored_distinct_departures']} distinct departures across {result['scored_distinct_trips']} trips**. "
           'A departure can occur at several horizons and in several tables; these are not independent observations.','',
           '## Inputs and rules','',
           '- Revision: change in the same event’s estimate over approximately three minutes. Extrapolate that rate for at most three minutes, capped at a ±120-second adjustment.',
           '- Earlier stop: estimated delay at the closest earlier stop in the same trip response, requiring the same bus ID. Its estimated passage must precede snapshot receipt by no more than ten minutes.',
           '- Blend: average the target API estimate and earlier-stop delay equally.',
           '- Combined: average the trend-adjusted API estimate and earlier-stop delay equally.','',
           '**Earlier-stop estimates are a proxy, not measured bus progress.** The capture has no actual stop-passage telemetry or vehicle positions. '
           'A past estimated passage does not confirm that the bus passed the stop. Estimated clock times are conservatively resolved using a dated target event known at the cutoff.','',
           'All snapshot receipt times precede the prediction cutoff. Current/history stop estimates must be fresh within 90 seconds of their respective cutoffs; '
           'trip snapshots must be no more than four minutes old. Missing or changed bus IDs, ambiguous dates, inconsistent schedules and stale data are excluded. '
           'Later measured outcomes are loaded only after feature extraction.','',
           '## Paired comparisons','',
           'MAE is in seconds; lower is better. Each row compares methods on exactly the same departures. '
           'Availability differs between rows and horizons. The larger upstream table is not directly comparable with the revision table.','']
    for name,(candidates,_) in cohorts.items():
        methods=['api']+candidates
        lines += [f'### {name.capitalize()} available','',
                  '| Minutes ahead | Departures | Trips | '+' | '.join(m+' MAE' for m in methods)+' |',
                  '|---|---:|---:|'+'---:|'*len(methods)]
        subset=[c for c in comparisons if c['cohort']==name]
        for c in subset:
            lines.append(f"| {c['horizon_minutes']} | {c['departures']} | {c['distinct_trips']} | "+
                         ' | '.join(f"{c['scores'][m]['mae_seconds']:.1f}" for m in methods)+' |')
        if not subset:lines.append('No eligible paired comparisons.')
        lines.append('')
    common=[c for c in comparisons if c['cohort']=='both']
    better=[str(c['horizon_minutes']) for c in common if c['scores']['combined']['mae_seconds']<c['scores']['api']['mae_seconds']]
    worse=[str(c['horizon_minutes']) for c in common if c['scores']['combined']['mae_seconds']>c['scores']['api']['mae_seconds']]
    lines += ['## What this run shows','',
              f"The combined rule has lower MAE at **{', '.join(better) or 'no'} minutes** and higher MAE at **{', '.join(worse) or 'no'} minutes** on the common-availability cohorts. "
              'The effect is inconsistent across horizons. Small near-departure gains warrant a larger future test, but do not justify replacing the API estimate. '
              'The separate tables above show whether any gain comes from estimate revisions, the earlier-stop proxy, or their combination.','']
    lines += ['## Interpretation limits','',
              'An improvement here would support collecting more data to test the signal; it would not validate a deployable model. '
              'A worse fixed rule would not prove the underlying feature is useless to a learned model. '
              'Rules may double-count information already incorporated into the API estimate, and a trend may not persist. '
              'There is insufficient independent data for a credible held-out learned-feature ablation. Reserve newly collected dates for that evaluation.','',
              'Only dated target stop events were scored. Their measured outcomes were conservatively linked by route, stop, destination and exact schedule time; '
              'unmatched outcomes remain missing. Multiple stops can belong to the same trip. No measured upstream outcomes are used as input.','',
              'Protocol, missing-feature counts, all extracted features and row-level scored predictions are saved alongside this report.','',
              '```bash',f'.venv/bin/python -m src.test_live_signals --run-directory {run_directory} \\',
              f'  --outcomes {outcomes_path} --output-directory {output_directory}','```','']
    (folder/'report.md').write_text('\n'.join(lines))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-directory',required=True);p.add_argument('--outcomes',required=True);p.add_argument('--output-directory',required=True)
    a=p.parse_args();result=run_experiment(a.run_directory,a.outcomes,a.output_directory)
    print(json.dumps({k:result[k] for k in ['scored_distinct_departures','scored_distinct_trips','revision_unavailable','upstream_unavailable','comparisons']},indent=2))
