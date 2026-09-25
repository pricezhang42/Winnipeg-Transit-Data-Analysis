"""Export credential-free trip snapshots and conservative measured-departure links for the dashboard."""
import argparse
from bisect import bisect_right
from collections import defaultdict,Counter
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode
from urllib.request import urlopen
from .transit_api import api_time,LOCAL_ZONE
from .live_signals import resolve_trip_schedule,estimate_near_schedule
from .evaluate_live import destination_key


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n')


def export(database,output,refresh=False):
    output=Path(output);cache=Path('data/dashboard_cache');cache.mkdir(parents=True,exist_ok=True)
    anchors=defaultdict(list);snapshots=[]
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        for row in db.execute('''SELECT s.received_at_utc,o.fields_json FROM observations o JOIN snapshots s USING(snapshot_id)
            WHERE s.origin='live' AND s.resource_type='stop' AND s.error_kind IS NULL ORDER BY s.received_at_utc'''):
            f=json.loads(row['fields_json']);dt,issue=api_time(f.get('scheduled_departure_raw'))
            if issue or not f.get('trip_key'):continue
            anchors[f['trip_key']].append({'received':row['received_at_utc'],'datetime':dt,'event':f['scheduled_stop_key'],
                                          'bus':f.get('bus_key'),'destination':f.get('variant_name') or f.get('destination'),
                                          'variant':f.get('variant_key')})
        snapshots=[dict(r) for r in db.execute("SELECT snapshot_id,received_at_utc,payload_json FROM snapshots WHERE origin='live' AND resource_type='trip' AND http_status=200 AND error_kind IS NULL ORDER BY received_at_utc")]
        latest=db.execute('SELECT MAX(received_at_utc) FROM snapshots').fetchone()[0]
    anchor_times={key:[r['received'] for r in rows] for key,rows in anchors.items()}
    trips={};skipped=Counter()
    for snapshot in snapshots:
        root=json.loads(snapshot['payload_json']).get('trip',{});key=str(root.get('key'));bus=str((root.get('bus') or {}).get('key',''))
        history=anchors.get(key,[]);limit=bisect_right(anchor_times.get(key,[]),snapshot['received_at_utc'])
        anchor=None;resolved=None;events=root.get('scheduled-stops',[])
        for candidate in reversed(history[max(0,limit-50):limit]):
            if candidate['bus']!=bus:continue
            if (datetime.fromisoformat(snapshot['received_at_utc'])-datetime.fromisoformat(candidate['received'])).total_seconds()>7200:continue
            try: _,resolved=resolve_trip_schedule(events,candidate['event'],candidate['datetime'])
            except (ValueError,KeyError,TypeError):continue
            anchor=candidate;break
        if anchor is None:
            skipped['no_unambiguous_dated_anchor']+=1;continue
        date=str(resolved[0].astimezone(LOCAL_ZONE).date());ident=f'{date}_{key}_{bus}'
        stops=[];times=[]
        for event,scheduled in zip(events,resolved):
            stop=event.get('stop',{});geo=(stop.get('centre') or {}).get('geographic') or {};t=event.get('times',{})
            def stamp(value):
                try:return int(estimate_near_schedule(value,scheduled).timestamp())
                except (ValueError,TypeError):return None
            departure=t.get('departure') or {};arrival=t.get('arrival') or {}
            stops.append({'event':str(event['key']),'key':str(stop.get('key','')),'number':str(stop.get('number',stop.get('key',''))),
                          'name':stop.get('name','Unnamed stop'),'lat':geo.get('latitude'),'lon':geo.get('longitude'),
                          'scheduled':int(scheduled.timestamp()),'scheduledArrival':stamp(arrival.get('scheduled'))})
            times.append([stamp(departure.get('estimated')),stamp(arrival.get('estimated')),str(event.get('cancelled')).lower() in ('true','1')])
        trip=trips.setdefault(ident,{'id':ident,'date':date,'trip':key,'bus':bus,'variant':str((root.get('variant') or {}).get('key','')),
                    'destination':anchor['destination'],'dayType':root.get('schedule-type'),'stops':stops,'snapshots':[]})
        if [s['event'] for s in stops] != [s['event'] for s in trip['stops']] or [s['scheduled'] for s in stops]!=[s['scheduled'] for s in trip['stops']]:
            skipped['schedule_changed_within_trip']+=1;continue
        if not trip['destination'] and anchor['destination']:trip['destination']=anchor['destination']
        trip['snapshots'].append({'at':int(datetime.fromisoformat(snapshot['received_at_utc']).timestamp()),'times':times})
    dates=sorted({t['date'] for t in trips.values()});outcomes=[];source_status={}
    for date in dates:
        file=cache/(date+'.json')
        if refresh or not file.exists():
            end=(datetime.fromisoformat(date)+timedelta(days=1)).date().isoformat()
            query={'$where':f"route_number='BLUE' AND scheduled_time >= '{date}T00:00:00' AND scheduled_time < '{end}T00:00:00'",'$limit':50000,'$order':'row_id'}
            try:
                url='https://data.winnipeg.ca/resource/gp3k-am4u.json?'+urlencode(query)
                with urlopen(url,timeout=45) as response: records=json.load(response)
                if len(records)>=50000:raise ValueError('Outcome query reached row limit; refusing incomplete data.')
                save(file,{'retrievedAt':datetime.now(timezone.utc).isoformat(),'url':url,'rows':records})
            except Exception as exc:
                source_status[date]={'error':type(exc).__name__,'rows':None};continue
        cached=json.loads(file.read_text());outcomes.extend(cached['rows'])
        source_status[date]={'rows':len(cached['rows']),'retrievedAt':cached['retrievedAt']}
    lookup=defaultdict(list);identities=defaultdict(set)
    def mk(stop,destination):
        return (stop['number'],destination_key(destination),stop['scheduled'])
    for trip in trips.values():
        for stop in trip['stops']:identities[mk(stop,trip['destination'])].add((trip['date'],trip['trip'],stop['event']))
    for row in outcomes:
        date,issue=api_time(row.get('scheduled_time'))
        if not issue:lookup[(row['stop_number'],destination_key(row.get('route_destination')),int(date.timestamp()))].append(row)
    summaries=[];measured=0
    for trip in trips.values():
        for stop in trip['stops']:
            key=mk(stop,trip['destination']);records=lookup.get(key,[])
            status='matched' if len(records)==1 and len(identities[key])==1 else 'ambiguous' if len(records)>1 or len(identities[key])>1 else 'unavailable'
            stop['matchStatus']=status;stop['actual']=None;stop['outcomeRowId']=None
            if status=='matched':
                delay=-float(records[0]['deviation']);stop['actual']=stop['scheduled']+delay;stop['outcomeRowId']=records[0]['row_id'];measured+=1
        values=[s['actual']-s['scheduled'] for s in trip['stops'] if s['actual'] is not None]
        summary={k:trip[k] for k in ['id','date','trip','bus','variant','destination','dayType']}
        summary.update({'start':trip['stops'][0]['scheduled'],'end':trip['stops'][-1]['scheduled'],
                        'from':trip['stops'][0]['name'],'to':trip['stops'][-1]['name'],'stops':len(trip['stops']),
                        'captures':len(trip['snapshots']),'firstCapture':trip['snapshots'][0]['at'],'lastCapture':trip['snapshots'][-1]['at'],
                        'measuredStops':len(values),'within120':sum(abs(v)<=120 for v in values)/len(values) if values else None,
                        'medianDelay':sorted(values)[len(values)//2] if values else None})
        summaries.append(summary);save(output/'trips'/(trip['id']+'.json'),trip)
    summaries.sort(key=lambda t:(t['date'],t['start'],t['trip']),reverse=True)
    default=max(summaries,key=lambda t:(t['measuredStops']>0,t['captures']>=5,t['measuredStops'],t['captures']))['id'] if summaries else None
    index={'generatedAt':datetime.now(timezone.utc).isoformat(),'latestCapture':latest,'route':'BLUE','timezone':'America/Winnipeg',
           'defaultTrip':default,'trips':summaries,'sourceStatus':source_status,'skippedSnapshots':dict(skipped),
           'notes':'Measured values are City departure records joined uniquely by stop, destination and schedule. Trip and bus identities come from captured API data. No measured arrivals.'}
    save(output/'index.json',index)
    print(json.dumps({'trips':len(summaries),'snapshots':sum(t['captures'] for t in summaries),'measuredStops':measured,'dates':dates,'sources':source_status,'skipped':dict(skipped)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database',default='data/live/transit.sqlite');p.add_argument('--output',default='dashboard/dist/data');p.add_argument('--refresh-outcomes',action='store_true')
    a=p.parse_args();export(a.database,a.output,a.refresh_outcomes)
