"""Export the prepared BLUE history as compact per-day dashboard assets, preserving quality flags."""
from datetime import datetime,timezone
import hashlib,json
from pathlib import Path
import pandas as pd
from .data import prepare_route, TIME_FORMAT

FLAGS=['flag_extreme_delay','flag_duplicate_candidate','flag_missing_destination','flag_missing_day_type','flag_invalid_location']

def export(source='data/processed/blue_clean.parquet',output='dashboard/dist/history'):
    source=Path(source);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    frame=pd.read_parquet(source)
    original_end=frame.scheduled_time_local.max().date().isoformat()
    supplemental=[]
    config=json.loads(Path('config.json').read_text())
    for file in sorted(Path('data/dashboard_cache').glob('????-??-??.json')):
        if file.stem<=original_end:continue
        cached=json.loads(file.read_text());records=[]
        for row in cached['rows']:
            if row.get('route_number')!='BLUE':continue
            coords=(row.get('location') or {}).get('coordinates',[])
            records.append({'Row ID':row.get('row_id'),'Stop Number':row.get('stop_number'),
                'Route Number':row.get('route_number'),'Route Name':row.get('route_name'),
                'Route Destination':row.get('route_destination'),'Day Type':row.get('day_type'),
                'Scheduled Time':pd.Timestamp(row['scheduled_time']).strftime(TIME_FORMAT),
                'Deviation':row.get('deviation'),'Location':f'POINT ({coords[0]} {coords[1]})' if len(coords)==2 else ''})
        if not records:continue
        extra=prepare_route(pd.DataFrame(records).astype('string'),config)
        assert extra.scheduled_time_local.dt.strftime('%Y-%m-%d').eq(file.stem).all()
        assert not set(extra.row_id)&set(frame.row_id), 'Overlapping source Row IDs'
        frame=pd.concat([frame,extra],ignore_index=True)
        supplemental.append({'date':file.stem,'records':len(extra),'retrievedAt':cached['retrievedAt'],'url':cached['url']})
    stops=sorted(frame.stop_number.dropna().astype(str).unique(),key=int)
    destinations=sorted(frame.destination.dropna().astype(str).unique());day_types=sorted(frame.day_type.dropna().astype(str).unique())
    maps=[{v:i for i,v in enumerate(values)} for values in [stops,destinations,day_types]]
    names={}
    for file in Path('dashboard/dist/data/trips').glob('*.json'):
        for stop in json.loads(file.read_text())['stops']:names[stop['number']]=stop['name']
    summary=[];exported=0
    frame['bits']=sum(frame[name].fillna(False).astype(int)*(1<<i) for i,name in enumerate(FLAGS))
    frame.loc[~frame.valid_required_fields,'bits']+=32
    for date,group in frame.groupby(frame.scheduled_time_local.dt.strftime('%Y-%m-%d'),dropna=False):
        if pd.isna(date):raise ValueError('Historical row lacks scheduled date; refusing silent loss.')
        rows=[]
        for r in group.itertuples():
            seconds=r.scheduled_time_local.hour*3600+r.scheduled_time_local.minute*60+r.scheduled_time_local.second
            delay=float(r.delay_seconds) if pd.notna(r.delay_seconds) else None
            if delay is not None and delay.is_integer():delay=int(delay)
            rows.append([str(r.row_id),maps[0].get(str(r.stop_number),-1),maps[1].get(str(r.destination),-1),seconds,delay,int(r.bits),maps[2].get(str(r.day_type),-1)])
        (output/(date+'.json')).write_text(json.dumps(rows,separators=(',',':'),allow_nan=False)+'\n')
        summary.append({'date':date,'rows':len(rows),'flagged':sum(bool(r[5]) for r in rows)});exported+=len(rows)
    assert exported==len(frame)
    index={'route':'BLUE','records':exported,'dates':summary,'stops':[{'number':s,'name':names.get(s)} for s in stops],
           'destinations':destinations,'dayTypes':day_types,'columns':['rowId','stopIndex','destinationIndex','secondsAfterMidnight','delaySeconds','qualityFlags','dayTypeIndex'],
           'flags':FLAGS+['invalid_required_fields'],'timezone':'America/Winnipeg',
           'sourceFile':'Recent_Transit_On-Time_Performance_Data_20260913.csv','sourceUrl':'https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u/about_data',
           'sourcePreparedSha256':hashlib.sha256(source.read_bytes()).hexdigest(),'exportedAt':datetime.now(timezone.utc).isoformat(),
           'supplementalSources':supplemental,
           'notes':'Measured departures. Positive delay means late. No bus IDs, trip IDs or reliable stop sequence exist in this file. All source rows and quality flags are retained.'}
    (output/'index.json').write_text(json.dumps(index,separators=(',',':'))+'\n')
    print(json.dumps({'records':exported,'days_with_records':len(summary),'first':summary[0]['date'],'last':summary[-1]['date'],'flagged':sum(x['flagged'] for x in summary),'bytes':sum(p.stat().st_size for p in output.glob('*.json'))}))

if __name__=='__main__':export()
