"""Audit recorded visits and export descriptive categories, never risk probabilities."""
import argparse, csv, json, sqlite3, tempfile, re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from passup_policy import POLICY, classify
from export_reliability import destination, point, distance, key, digest

def seconds(dt):
    return int((dt-datetime(1970,1,1)).total_seconds())

def choose_stop(coords, stops, radius=60, gap=30):
    ranked=sorted((distance(coords,p),stop) for stop,p in stops.items())
    if not ranked or ranked[0][0]>radius:return None,'no_nearby_stop',ranked[:2]
    if len(ranked)>1 and ranked[1][0]-ranked[0][0]<gap:return None,'ambiguous_stop',ranked[:2]
    return ranked[0][1],None,ranked[:2]

def choose_visit(ts, visits, window=180, gap=60):
    ranked=sorted((abs(v[1]-ts),v[0]) for v in visits)
    if not ranked or ranked[0][0]>window:return None,'no_nearby_visit',ranked[:2]
    if len(ranked)>1 and ranked[1][0]-ranked[0][0]<gap:return None,'ambiguous_visit',ranked[:2]
    return ranked[0][1],None,ranked[:2]

def build(departures, passups, output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    audit=Counter();coordinates=defaultdict(dict);coordinate_conflicts=set();dates=set()
    with tempfile.TemporaryDirectory(prefix='passup-visits-') as tmp:
        db=sqlite3.connect(str(Path(tmp)/'visits.sqlite'))
        db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF')
        db.execute('CREATE TABLE visits(id INTEGER PRIMARY KEY, source TEXT UNIQUE, route TEXT, stop TEXT, dest TEXT, scheduled TEXT, actual INTEGER, groupkey TEXT, day TEXT, valid INTEGER DEFAULT 1, UNIQUE(route,stop,dest,scheduled))')
        with open(departures,encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):
                audit['departureRows']+=1
                try:
                    dt=datetime.strptime(row['Scheduled Time'],'%Y %b %d %I:%M:%S %p')
                    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)',row['Deviation']):raise ValueError()
                    # Source positive means early. Do not discard late buses from this denominator.
                    actual=seconds(dt)-int(row['Deviation'].replace(',',''))
                    route=row['Route Number'];stop=row['Stop Number'];dest=destination(row['Route Destination']);source=row['Row ID']
                    if not all([route,stop,dest,source]):raise ValueError()
                except (ValueError,TypeError):audit['invalidDepartureRows']+=1;continue
                day=dt.date().isoformat();dates.add(day)
                expected='Saturday' if dt.weekday()==5 else 'Sunday' if dt.weekday()==6 else 'Weekday'
                if row['Day Type']!=expected:audit['nonCalendarServiceRows']+=1;continue
                coords=point(row['Location']);identity=(route,dest,stop)
                if coords:
                    prior=coordinates[(route,dest)].get(stop)
                    if prior and distance(prior,coords)>30:coordinate_conflicts.add(identity)
                    if not prior:coordinates[(route,dest)][stop]=coords
                values=(source,route,stop,dest,dt.isoformat(),actual,key(route,stop,dest,dt),day)
                cursor=db.execute('INSERT OR IGNORE INTO visits(source,route,stop,dest,scheduled,actual,groupkey,day) VALUES(?,?,?,?,?,?,?,?)',values)
                if cursor.rowcount==0:
                    prior=db.execute('SELECT id,actual FROM visits WHERE route=? AND stop=? AND dest=? AND scheduled=?',(route,stop,dest,dt.isoformat())).fetchone()
                    if prior and prior[1]!=actual:
                        audit['conflictingDepartureRows']+=1;db.execute('UPDATE visits SET valid=0 WHERE id=?',(prior[0],))
                    elif prior:audit['duplicateVisitRows']+=1
                    else:audit['duplicateSourceIds']+=1
                if audit['departureRows']%100000==0:db.commit()
        db.commit()
        db.execute('CREATE INDEX actual_lookup ON visits(route,stop,dest,actual)')
        audit['uniqueRecordedVisits']=db.execute('SELECT count(*) FROM visits WHERE valid=1').fetchone()[0]
        audit['conflictedVisitsExcluded']=db.execute('SELECT count(*) FROM visits WHERE valid=0').fetchone()[0]
        audit['unstableStopCoordinates']=len(coordinate_conflicts)
        rows=[];seen=set();matched=set();matched_groups=Counter();sensitivity=Counter();by_route=defaultdict(Counter);profile_visits=defaultdict(set);uncertain_groups=set()
        with open(passups,encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):
                if row['Pass-Up Type']!='Full Bus Pass-Up':continue
                try:dt=datetime.strptime(row['Time'],'%m/%d/%Y %I:%M:%S %p')
                except ValueError:audit['invalidReportTimes']+=1;continue
                if dt.date().isoformat() not in dates:continue
                audit['reportsOnSourceDates']+=1
                if row['Pass-Up ID'] in seen:audit['duplicateReportIds']+=1;continue
                seen.add(row['Pass-Up ID']);route=row['Route Number'];dest=destination(row['Route Destination']);coords=point(row['Location']);ts=seconds(dt)
                result={'reportId':row['Pass-Up ID'],'route':route,'destination':dest,'reportTime':dt.isoformat(),'longitude':coords[0] if coords else None,'latitude':coords[1] if coords else None}
                candidates=coordinates.get((route,dest),{})
                if not coords:
                    result['status']='invalid_coordinates';rows.append(result);audit[result['status']]+=1;continue
                stop,reason,ranked=choose_stop(coords,candidates)
                result.update(nearestStop=ranked[0][1] if ranked else None,nearestMeters=round(ranked[0][0],1) if ranked else None,secondMeters=round(ranked[1][0],1) if len(ranked)>1 else None)
                # Sensitivity is diagnostic only: never tune production thresholds from match counts alone.
                for radius in (60,100,150,300):
                    candidate,why,_=choose_stop(coords,candidates,radius)
                    if candidate and (route,dest,candidate) not in coordinate_conflicts:
                        visits=db.execute('SELECT id,actual FROM visits WHERE route=? AND stop=? AND dest=? AND valid=1 AND actual BETWEEN ? AND ?',(route,candidate,dest,ts-300,ts+300)).fetchall()
                        for window in (120,180,300):
                            selected=choose_visit(ts,visits,window)[0]
                            if selected is not None:
                                profile=f'{radius}m_{window}s';sensitivity[profile]+=1
                                profile_visits[profile].add(selected)
                # A nearby report that cannot be assigned even under the broad
                # profile must not silently count as evidence for Low.
                broad_stop,broad_reason,broad_ranked=choose_stop(coords,candidates,150)
                broad_visit=None
                if broad_stop and (route,dest,broad_stop) not in coordinate_conflicts:
                    broad_candidates=db.execute('SELECT id,actual FROM visits WHERE route=? AND stop=? AND dest=? AND valid=1 AND actual BETWEEN ? AND ?',(route,broad_stop,dest,ts-300,ts+300)).fetchall()
                    broad_visit=choose_visit(ts,broad_candidates,300)[0]
                if broad_visit is None:
                    for meters,uncertain_stop in broad_ranked:
                        if meters<=150:
                            for hour_offset in (-1,0,1):
                                uncertain_groups.add(key(route,uncertain_stop,dest,dt+timedelta(hours=hour_offset)))
                if not reason and (route,dest,stop) in coordinate_conflicts:reason='unstable_stop_coordinates'
                if not reason:
                    visits=db.execute('SELECT id,actual FROM visits WHERE route=? AND stop=? AND dest=? AND valid=1 AND actual BETWEEN ? AND ?',(route,stop,dest,ts-300,ts+300)).fetchall()
                    visit,reason,timing=choose_visit(ts,visits)
                    result['nearestVisitSeconds']=timing[0][0] if timing else None
                    if not reason:
                        row_id,g,scheduled,actual=db.execute('SELECT id,groupkey,scheduled,actual FROM visits WHERE id=?',(visit,)).fetchone()
                        result.update(matchedVisit=row_id,scheduledTime=scheduled,actualTime=(datetime(1970,1,1)+timedelta(seconds=actual)).isoformat(),groupKey=g)
                        if visit in matched:reason='duplicate_report_for_visit'
                        else:matched.add(visit);matched_groups[g]+=1
                result['status']=reason or 'matched';audit[result['status']]+=1;by_route[route][result['status']]+=1;rows.append(result)
        db.execute('CREATE INDEX group_lookup ON visits(groupkey,valid)')
        groups=[]
        for g,n in matched_groups.items():
            denominator,days=db.execute('SELECT count(*),count(DISTINCT day) FROM visits WHERE groupkey=? AND valid=1',(g,)).fetchone()
            groups.append({'group':json.loads(g),'matchedReportedVisits':n,'recordedVisits':denominator,'distinctDates':days,'experimentalReportedRate':n/denominator,'publishable':False})
        report={'status':'audit_only','coverageStart':min(dates),'coverageEnd':max(dates),'audit':dict(audit),'byRoute':dict(by_route),'sensitivityMatchedReports':dict(sensitivity),
          'parameters':{'radiusMeters':60,'stopSeparationMeters':30,'timeWindowSeconds':180,'timeSeparationSeconds':60},
          'sources':{'departures':{'name':Path(departures).name,'sha256':digest(departures)},'passups':{'name':Path(passups).name,'sha256':digest(passups)}},
          'publicationBlockedBy':['Unknown whether passed stops generate departure records','Unknown operator reporting completeness','No shared trip identifier; spatial and temporal matches require manual validation','Recorded departures are not confirmed complete actual visits'],
          'groupsWithMatchedReports':groups}
        (output/'audit.json').write_text(json.dumps(report,indent=2)+'\n')
        fields=sorted({k for r in rows for k in r})
        with (output/'matches.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
        profiles=defaultdict(dict)
        for profile in POLICY['profiles']:
            counts=Counter();days=defaultdict(set)
            for visit in profile_visits[profile]:
                g,d=db.execute('SELECT groupkey,day FROM visits WHERE id=?',(visit,)).fetchone()
                counts[g]+=1;days[g].add(d)
            for g,n in counts.items():profiles[g][profile]=(n,len(days[g]))
        risk_groups={};labels=Counter();reasons=Counter()
        for g,n,days,start,end in db.execute('SELECT groupkey,count(*),count(DISTINCT day),min(day),max(day) FROM visits WHERE valid=1 GROUP BY groupkey'):
            identity=json.loads(g)
            unstable=(identity[0],identity[2],identity[1]) in coordinate_conflicts
            label,reason=classify(n,days,profiles[g],unstable)
            labels[label]+=1;reasons[reason]+=1
            if label!='unknown':
                risk_groups[g]={'level':label,'recordedVisits':n,'distinctDates':days,'coverageStart':start,'coverageEnd':end,'profiles':{p:{'reportedVisits':profiles[g].get(p,(0,0))[0],'reportDates':profiles[g].get(p,(0,0))[1]} for p in POLICY['profiles']}}
        risk={'schemaVersion':1,'coverageStart':min(dates),'coverageEnd':max(dates),'policy':POLICY,'sources':report['sources'],'labelCounts':dict(labels),'reasonCounts':dict(reasons),'groups':risk_groups,'interpretation':'Relative historical reported pass-up patterns, not a probability or a guarantee of boarding.'}
        (output/'passup-risk.json').write_text(json.dumps(risk,separators=(',',':'))+'\n')
        (output/'passup-risk.report.json').write_text(json.dumps({k:v for k,v in risk.items() if k!='groups'},indent=2)+'\n')
        print(json.dumps({'labels':dict(labels),'reasons':dict(reasons)},indent=2))
        db.close()
        print(json.dumps({'audit':dict(audit),'sensitivity':dict(sensitivity),'groups':len(groups)},indent=2))
        return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--departures',required=True);parser.add_argument('--passups',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();build(args.departures,args.passups,args.output)
