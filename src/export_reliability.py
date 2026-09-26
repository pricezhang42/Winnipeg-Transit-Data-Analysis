"""Export a descriptive reliability network. No fitted arrival or pass-up probability claims."""
import argparse
import csv
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from array import array
import hashlib
import json
import math
from pathlib import Path
import random
import re

VERSION = 1
MIN_COUNT = 30
MIN_DAYS = 5

def season(month):
    return 'winter' if month in (11,12,1,2,3) else 'summer' if month in (6,7,8) else 'transition'

def destination(value):
    return re.sub(r'^to\s+', '', (value or '').strip().lower())

def key(route, stop, dest, dt):
    # Fixed one-hour bins centered on HH:30; midnight is not mixed with another date.
    return json.dumps([str(route), str(stop), destination(dest), 'weekend' if dt.weekday() >= 5 else 'weekday', season(dt.month), dt.hour], separators=(',', ':'))

def point(value):
    m = re.fullmatch(r'POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)', value or '')
    if not m: return None
    lon, lat = map(float, m.groups())
    return (lon, lat) if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90 else None

def distance(a,b):
    lon1,lat1,lon2,lat2=map(math.radians,(*a,*b))
    v=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742000*math.asin(min(1,math.sqrt(v)))

def quantile(values, q):
    at=(len(values)-1)*q;lo=int(at);hi=min(lo+1,len(values)-1)
    return round(values[lo]+(values[hi]-values[lo])*(at-lo),2)

def summary(values, days, intervals=True):
    values=sorted(values);n=len(values)
    counts=[sum(x < -60 for x in values),sum(-60 <= x <= 300 for x in values),sum(x > 300 for x in values)]
    result={'observations':n,'distinctDates':len(days),'coverageStart':min(days),'coverageEnd':max(days),
            'medianSeconds':quantile(values,.5),'p10Seconds':quantile(values,.1),'p90Seconds':quantile(values,.9),
            'earlyShare':counts[0]/n,'withinShare':counts[1]/n,'lateShare':counts[2]/n,'beforeScheduleShare':sum(x<0 for x in values)/n}
    # Resample whole dates instead of pretending same-day departures are independent.
    if intervals and len(days)>=MIN_DAYS:
        rng=random.Random(20260925);daily=list(days.values());samples=[[],[],[]]
        for _ in range(100):
            picked=rng.choices(daily,k=len(daily));denom=sum(d[0] for d in picked)
            for j in range(3): samples[j].append(sum(d[j+1] for d in picked)/denom)
        result['shareIntervals']=[[quantile(sorted(s),.025),quantile(sorted(s),.975)] for s in samples]
    return result

def add(groups, k, delay, date):
    if k not in groups: groups[k]={'values':array('i'),'days':defaultdict(lambda:[0,0,0,0])}
    group=groups[k];group['values'].append(delay);day=group['days'][date];day[0]+=1
    day[1 if delay < -60 else 3 if delay > 300 else 2]+=1

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def build(otp,passups,output,routes):
    observed_routes=set();groups={};by_route=defaultdict(list);audit=Counter();seen=set();dates=Counter();stops=defaultdict(dict)
    # Stream all routes for coverage; retain only the network observations in memory.
    with open(otp,encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            audit['sourceRows']+=1
            if routes is not None and row['Route Number'] not in routes:continue
            audit['networkRows']+=1
            try:
                dt=datetime.strptime(row['Scheduled Time'],'%Y %b %d %I:%M:%S %p');d=dt.date().isoformat()
                raw=row['Deviation']
                if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)',raw):raise ValueError()
                delay=-int(raw.replace(',',''))
            except (ValueError,TypeError):audit['invalidRows']+=1;continue
            if row['Row ID'] in seen:audit['duplicateIds']+=1;continue
            seen.add(row['Row ID'])
            if abs(delay)>3600:audit['extremeDeviationRows']+=1;continue
            if not row['Stop Number'] or not row['Route Destination']:audit['missingIdentityRows']+=1;continue
            expected='Saturday' if dt.weekday()==5 else 'Sunday' if dt.weekday()==6 else 'Weekday'
            if row['Day Type']!=expected:audit['nonCalendarServiceRows']+=1;continue
            route=row['Route Number'];observed_routes.add(route);stop=row['Stop Number'];dest=destination(row['Route Destination'])
            k=key(route,stop,dest,dt);add(groups,k,delay,d);dates[d]+=1
            by_route[route].append((k,delay,d))
            coords=point(row['Location'])
            if coords:stops[(route,dest)][stop]=coords
    if not dates:raise ValueError('No usable network data')
    cutoff=(datetime.fromisoformat(max(dates))-timedelta(days=6)).date().isoformat()
    training={};holdout=[]
    for route,rows in by_route.items():
        for k,delay,d in rows:
            if d<cutoff:add(training,k,delay,d)
            else:holdout.append((k,delay,d))
    frozen={k:summary(g['values'],g['days'],intervals=False) for k,g in training.items() if len(g['values'])>=MIN_COUNT and len(g['days'])>=MIN_DAYS}
    matched=0;mae=0;brier=0;covered=0
    for k,delay,d in holdout:
        model=frozen.get(k)
        if not model:continue
        matched+=1;mae+=abs(delay-model['medianSeconds']);covered+=model['p10Seconds']<=delay<=model['p90Seconds']
        target=[delay < -60,-60 <= delay <=300,delay>300]
        brier+=sum((p-y)**2 for p,y in zip([model['earlyShare'],model['withinShare'],model['lateShare']],target))
    evaluation={'holdoutStart':cutoff,'holdoutEnd':max(dates),'holdoutRows':len(holdout),'matchedRows':matched,'matchedCoverage':matched/len(holdout) if holdout else 0,
                'medianMAESeconds':mae/matched if matched else None,'multiclassBrierScore':brier/matched if matched else None,'middle80Coverage':covered/matched if matched else None,
                'note':'Chronological retrospective network; these dates are now inspected. Descriptive summaries, not a validated future forecast. No arrival outcomes.'}
    reports=Counter();report_audit=Counter()
    with open(passups,encoding='utf-8-sig',newline='') as f:
        seen_reports=set()
        for row in csv.DictReader(f):
            report_audit['sourceRows']+=1
            if row['Pass-Up Type']!='Full Bus Pass-Up':continue
            if routes is not None and row['Route Number'] not in routes:continue
            dt=datetime.strptime(row['Time'],'%m/%d/%Y %I:%M:%S %p');d=dt.date().isoformat()
            if d not in dates:continue
            report_audit['networkReportsOnObservedDates']+=1
            if row['Pass-Up ID'] in seen_reports:report_audit['duplicateIds']+=1;continue
            seen_reports.add(row['Pass-Up ID']);coords=point(row['Location'])
            if not coords:report_audit['invalidCoordinates']+=1;continue
            candidates=stops.get((row['Route Number'],destination(row['Route Destination'])),{})
            ranked=sorted((distance(coords,p),stop) for stop,p in candidates.items())
            if not ranked or ranked[0][0]>60:report_audit['noNearbyCompatibleStop']+=1;continue
            if len(ranked)>1 and ranked[1][0]-ranked[0][0]<30:report_audit['ambiguousStop']+=1;continue
            k=key(row['Route Number'],ranked[0][1],row['Route Destination'],dt)
            # Include only dates observed in this exact group, not network-wide dates.
            if k not in groups or d not in groups[k]['days']:report_audit['noMatchingGroupDate']+=1;continue
            reports[k]+=1;report_audit['proximityMatchedReports']+=1
    result_groups={}
    for k,g in groups.items():
        if len(g['values'])<MIN_COUNT or len(g['days'])<MIN_DAYS:continue
        stats=summary(g['values'],g['days']);stats['nearbyFullBusReports']=reports[k];result_groups[k]=stats
    result={'schemaVersion':VERSION,'generatedAt':datetime.now(timezone.utc).isoformat(),'timezone':'America/Winnipeg',
            'coverageStart':min(dates),'coverageEnd':max(dates),'routes':sorted(observed_routes),'minObservations':MIN_COUNT,'minDates':MIN_DAYS,
            'earlyThresholdSeconds':-60,'lateThresholdSeconds':300,'groupWindowMinutes':60,
            'sources':{'departures':{'file':Path(otp).name,'sha256':digest(otp)},'passups':{'file':Path(passups).name,'sha256':digest(passups)}},
            'audit':dict(audit),'passUpAudit':dict(report_audit),'evaluation':evaluation,
            'limitations':['Historical observed departures, not arrival or trip-success probabilities.','Pass-up stop assignment is approximate and has not been manually validated.','Nearby reports are not a pass-up probability.','Holiday service and winter timing are unavailable unless covered in a future validated export.'],
            'groups':result_groups}
    out=Path(output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,separators=(',',':'),allow_nan=False)+'\n')
    report={k:v for k,v in result.items() if k!='groups'};report['eligibleGroups']=len(result_groups)
    out.with_suffix('.report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'groups':len(result_groups),'bytes':out.stat().st_size,'evaluation':evaluation,'passUpAudit':dict(report_audit)},indent=2))
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--departures',required=True);parser.add_argument('--passups',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--routes',nargs='+',default=None,help='Optional route subset; default includes every route in the source')
    args=parser.parse_args();build(args.departures,args.passups,args.output,set(args.routes) if args.routes else None)
