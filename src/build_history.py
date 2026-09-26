"""Build post-overhaul history from monthly ZIPs and overlapping recent CSVs (DuckDB)."""
import argparse,csv,hashlib,io,json,math,zipfile,shutil
from pathlib import Path
from datetime import datetime,timedelta
from collections import defaultdict,Counter
from urllib.parse import quote
import duckdb
from export_reliability import digest,point,distance,destination
from passup_policy import POLICY,classify

START='2025-06-29'
GROUPING='route-stop-destination-daytype-2hour-v1'
WINDOW_MINUTES=120
def bin_hour(hour):
    # Fixed two-hour bins keyed by their even start hour: 16 covers 16:00-17:59.
    return hour-hour%2
def key(route,stop,dest,dt):
    return json.dumps([route,str(stop),dest,'weekend' if dt.weekday()>=5 else 'weekday',bin_hour(dt.hour)],separators=(',',':'))
def write(path,value):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(value,separators=(',',':'),default=str,allow_nan=False)+'\n')
def connect(work):
    db=duckdb.connect(str(work/'history.duckdb'))
    db.execute("SET memory_limit='1GB'; SET threads=2; SET preserve_insertion_order=false")
    return db

def ingest(files,work,resume=False):
    db=connect(work);manifest=[]
    if not resume:db.execute('CREATE OR REPLACE TABLE raw(id VARCHAR,route VARCHAR,stop VARCHAR,dest VARCHAR,scheduled TIMESTAMP,delay BIGINT,service VARCHAR,lon DOUBLE,lat DOUBLE)')
    for path in files:
        path=Path(path);print('Importing '+path.name,flush=True)
        manifest.append({'file':path.name,'sha256':digest(path),'bytes':path.stat().st_size})
        if resume:continue
        paths=[path]
        if path.suffix.lower()=='.zip':
            z=zipfile.ZipFile(path);members=[n for n in z.namelist() if n.lower().endswith('.csv')]
            if len(members)!=1:raise ValueError('Expected one CSV in '+str(path))
            extracted=work/'current.csv'
            with z.open(members[0]) as src,extracted.open('wb') as dst:shutil.copyfileobj(src,dst)
            paths=[extracted]
        db.execute('''INSERT INTO raw SELECT "Row ID",trim("Route Number"),trim("Stop Number"),regexp_replace(lower(trim("Route Destination")),'^to\\s+',''),
          coalesce(try_cast("Scheduled Time" AS TIMESTAMP),try_strptime("Scheduled Time",'%Y %b %d %I:%M:%S %p')),
          -try_cast(replace("Deviation",',','') AS BIGINT),"Day Type",
          try_cast(regexp_extract("Location",'POINT\\s*\\(\\s*([-+0-9.eE]+)\\s+([-+0-9.eE]+)\\s*\\)',1) AS DOUBLE),
          try_cast(regexp_extract("Location",'POINT\\s*\\(\\s*([-+0-9.eE]+)\\s+([-+0-9.eE]+)\\s*\\)',2) AS DOUBLE)
          FROM read_csv(?,header=true,all_varchar=true)''',[str(paths[0])])
        if path.suffix.lower()=='.zip':extracted.unlink();z.close()
        db.execute('CHECKPOINT')
    if resume:
        expected=json.loads((work/'import-complete.json').read_text())
        if expected!=manifest:raise ValueError('Resume input files do not match the completed import')
    else:write(work/'import-complete.json',manifest)
    audit={'sourceRows':db.execute('SELECT count(*) FROM raw').fetchone()[0]}
    audit['beforeOverhaulRows']=db.execute('SELECT count(*) FROM raw WHERE scheduled < ?::TIMESTAMP',[START]).fetchone()[0]
    audit['invalidRows']=db.execute("SELECT count(*) FROM raw WHERE scheduled IS NULL OR delay IS NULL OR id IS NULL OR route IS NULL OR stop IS NULL OR dest IS NULL OR id='' OR route='' OR stop='' OR dest='' OR abs(delay)>604800").fetchone()[0]
    db.execute('''CREATE OR REPLACE TABLE unique_rows AS SELECT DISTINCT id,route,stop,dest,scheduled,delay,service,lon,lat FROM raw
      WHERE scheduled>=?::TIMESTAMP AND delay IS NOT NULL AND abs(delay)<=604800 AND coalesce(id,'')<>'' AND coalesce(route,'')<>'' AND coalesce(stop,'')<>'' AND coalesce(dest,'')<>'' ''',[START])
    audit['distinctSourceRows']=db.execute('SELECT count(*) FROM unique_rows').fetchone()[0]
    db.execute('CREATE OR REPLACE TABLE conflict_ids AS SELECT id FROM unique_rows GROUP BY id HAVING count(DISTINCT (route,stop,dest,scheduled,delay,service))>1')
    audit['conflictingSourceIds']=db.execute('SELECT count(*) FROM conflict_ids').fetchone()[0]
    db.execute('''CREATE OR REPLACE TABLE clean AS SELECT * FROM unique_rows ANTI JOIN conflict_ids USING(id)
      WHERE service=CASE WHEN dayofweek(scheduled)=6 THEN 'Saturday' WHEN dayofweek(scheduled)=0 THEN 'Sunday' ELSE 'Weekday' END''')
    audit['calendarMismatchRows']=db.execute("SELECT count(*) FROM unique_rows WHERE service IS DISTINCT FROM CASE WHEN dayofweek(scheduled)=6 THEN 'Saturday' WHEN dayofweek(scheduled)=0 THEN 'Sunday' ELSE 'Weekday' END").fetchone()[0]
    db.execute('''CREATE OR REPLACE TABLE identities AS SELECT route,stop,dest,scheduled,min(delay) delay,min(delay)<>max(delay) is_conflict,
      avg(lon) lon,avg(lat) lat,count(*) copies FROM clean GROUP BY route,stop,dest,scheduled''')
    audit['conflictingVisitIdentities']=db.execute('SELECT count(*) FROM identities WHERE is_conflict').fetchone()[0]
    audit['collapsedConsistentCopies']=db.execute('SELECT coalesce(sum(copies-1),0) FROM identities WHERE NOT is_conflict').fetchone()[0]
    db.execute('''CREATE OR REPLACE TABLE visits AS SELECT *,cast(json_array(route,stop,dest,daytype,hour) AS VARCHAR) g FROM (SELECT row_number() OVER() AS vid,route,stop,dest,scheduled,delay,lon,lat,
      scheduled+delay*INTERVAL 1 SECOND actual,cast(scheduled AS DATE) AS day,
      cast(scheduled+delay*INTERVAL 1 SECOND AS DATE) actual_day,
      CASE WHEN dayofweek(scheduled) IN (0,6) THEN 'weekend' ELSE 'weekday' END daytype,
      CASE WHEN month(scheduled) IN (11,12,1,2,3) THEN 'winter' WHEN month(scheduled) IN (6,7,8) THEN 'summer' ELSE 'transition' END season,
      cast(hour(scheduled)-hour(scheduled)%2 AS INTEGER) AS hour FROM identities WHERE NOT is_conflict)''')
    # JSON retains a numeric hour: the even start hour of the two-hour bin.
    lo,hi,n=db.execute('SELECT min(day),max(day),count(*) FROM visits').fetchone()
    audit['retainedRecordedVisits']=n
    coverage=db.execute('SELECT day,count(*) FROM visits GROUP BY day ORDER BY day').fetchall()
    bundle=hashlib.sha256(json.dumps({'files':manifest,'cutoff':START},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    meta={'schemaVersion':2,'coverageStart':str(lo),'coverageEnd':str(hi),'overhaulCutoff':START,'audit':audit,'sources':{'departures':{'sha256':bundle,'files':manifest}},'dailyCoverage':[{'date':str(d),'visits':n} for d,n in coverage]}
    write(work/'source-report.json',meta)
    for name in ['raw','unique_rows','conflict_ids','clean','identities']:db.execute('DROP TABLE '+name)
    db.execute('CHECKPOINT');db.close();print('Imported '+str(n)+' deduplicated visits',flush=True)

PROFILES=[('60m_120s',60,120),('100m_180s',100,180),('150m_300s',150,300)]
def match_reports(db,passups,meta,work):
    audit=Counter();reports=[];seen=set();dates={r['date'] for r in meta['dailyCoverage']}
    with open(passups,encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            audit['sourceReports']+=1
            if row['Pass-Up Type']!='Full Bus Pass-Up':continue
            dt=datetime.strptime(row['Time'],'%m/%d/%Y %I:%M:%S %p')
            if dt.date().isoformat() not in dates:continue
            audit['reportsOnCoveredDates']+=1
            rid=row['Pass-Up ID']
            if rid in seen:audit['duplicateReports']+=1;continue
            seen.add(rid);coords=point(row['Location'])
            if not coords:audit['invalidCoordinates']+=1;continue
            reports.append((rid,row['Route Number'],destination(row['Route Destination']),dt,dt.date(),*coords))
    db.execute('CREATE OR REPLACE TABLE reports(rid VARCHAR,route VARCHAR,dest VARCHAR,reported TIMESTAMP,day DATE,lon DOUBLE,lat DOUBLE)')
    db.executemany('INSERT INTO reports VALUES(?,?,?,?,?,?,?)',reports)
    print('Matching '+str(len(reports))+' reports with date-specific route stops',flush=True)
    db.execute('''CREATE OR REPLACE TABLE day_stops AS SELECT v.route,v.dest,v.day,v.stop,avg(v.lon) lon,avg(v.lat) lat,
      min(v.lon) lo_lon,max(v.lon) hi_lon,min(v.lat) lo_lat,max(v.lat) hi_lat
      FROM grouped_visits v SEMI JOIN (SELECT DISTINCT route,dest,day FROM reports) r USING(route,dest,day)
      WHERE v.lon BETWEEN -180 AND 180 AND v.lat BETWEEN -90 AND 90 GROUP BY v.route,v.dest,v.day,v.stop''')
    stops=defaultdict(dict);unstable=set()
    for route,dest,day,stop,lon,lat,x0,x1,y0,y1 in db.execute('SELECT * FROM day_stops').fetchall():
        stops[(route,dest,day)][stop]=(lon,lat)
        if distance((x0,y0),(x1,y1))>30:unstable.add((route,dest,day,stop))
    candidates=[];rankings={};uncertain=set()
    for rid,route,dest,dt,day,lon,lat in reports:
        ranked=sorted((distance((lon,lat),p),s) for s,p in stops[(route,dest,day)].items())[:2]
        rankings[rid]=ranked
        okay=ranked and ranked[0][0]<=150 and (len(ranked)==1 or ranked[1][0]-ranked[0][0]>=30) and (route,dest,day,ranked[0][1]) not in unstable
        if okay:candidates.append((rid,route,dest,ranked[0][1],dt,day,ranked[0][0]))
        else:
            audit['noUnambiguousNearbyStop']+=1
            for meters,stop in ranked:
                if meters<=150:
                    for h in [-1,0,1]:uncertain.add(key(route,stop,dest,dt+timedelta(hours=h)))
    db.execute('CREATE OR REPLACE TABLE candidates(rid VARCHAR,route VARCHAR,dest VARCHAR,stop VARCHAR,reported TIMESTAMP,day DATE,meters DOUBLE)')
    if candidates:db.executemany('INSERT INTO candidates VALUES(?,?,?,?,?,?,?)',candidates)
    db.execute('''CREATE OR REPLACE TABLE timed AS SELECT c.rid,v.vid,v.g,v.day,abs(epoch(v.actual)-epoch(c.reported)) delta,
       row_number() OVER(PARTITION BY c.rid ORDER BY abs(epoch(v.actual)-epoch(c.reported)),v.vid) ranking
       FROM (SELECT *,cast(day+delta*INTERVAL 1 DAY AS DATE) join_day FROM candidates CROSS JOIN (VALUES(-1),(0),(1)) d(delta)) c
       JOIN grouped_visits v ON c.route=v.route AND c.dest=v.dest AND c.stop=v.stop AND c.join_day=v.actual_day
       WHERE abs(epoch(v.actual)-epoch(c.reported))<=300 QUALIFY ranking<=2''')
    timed=defaultdict(list)
    for rid,vid,g,day,delta,ranking in db.execute('SELECT * FROM timed ORDER BY rid,ranking').fetchall():timed[rid].append((vid,g,day,delta))
    accepted=defaultdict(dict);match_rows=[]
    for rid,route,dest,stop,dt,day,meters in candidates:
        t=timed[rid];okay=t and (len(t)==1 or t[1][3]-t[0][3]>=60)
        if not okay:
            audit['noUnambiguousTimedVisit']+=1
            for h in [-1,0,1]:uncertain.add(key(route,stop,dest,dt+timedelta(hours=h)))
        for profile,radius,window in PROFILES:
            if okay and meters<=radius and t[0][3]<=window:
                vid,g,visit_day,delta=t[0];accepted[profile][vid]=(g,visit_day);audit[profile+'Reports']+=1
                match_rows.append({'report':rid,'profile':profile,'visit':vid,'group':g,'date':str(visit_day),'distanceMeters':round(meters,2),'timeSeconds':delta})
    profile_groups=defaultdict(dict)
    for profile,visits in accepted.items():
        count=Counter();days=defaultdict(set)
        for g,day in visits.values():count[g]+=1;days[g].add(day)
        for g,n in count.items():profile_groups[g][profile]=(n,len(days[g]))
        audit[profile+'DistinctVisits']=len(visits)
    write(work/'matches.json',match_rows);write(work/'passup-audit.json',dict(audit))
    return profile_groups,uncertain,dict(audit)

def export(db,meta,passups,out,work):
    # Recompute keys from raw visits even when reusing a previously seasonal database.
    db.execute("CREATE OR REPLACE TEMP VIEW grouped_visits AS SELECT * EXCLUDE(g),cast(json_array(route,stop,dest,daytype,hour-hour%2) AS VARCHAR) g FROM visits")
    # Unresolved reports stay in the audit but no longer affect labels (reported-pattern-v2).
    profiles,_,report_audit=match_reports(db,passups,meta,work)
    meta['sources']['passups']={'file':Path(passups).name,'sha256':digest(passups)}
    routes=[r[0] for r in db.execute('SELECT DISTINCT route FROM visits ORDER BY route').fetchall()]
    timing_files={};risk_files={};labels=Counter();reasons=Counter();total_groups=0
    for route in routes:
        print('Summarizing '+route,flush=True)
        db.execute('CREATE OR REPLACE TEMP TABLE route_visits AS SELECT * FROM grouped_visits WHERE route=?',[route])
        # Timing excludes very large deviations; visit denominators deliberately do not.
        db.execute('''CREATE OR REPLACE TEMP TABLE daily AS SELECT g,day,count(*) n,count(*) FILTER(WHERE delay < -60) e,
          count(*) FILTER(WHERE delay BETWEEN -60 AND 300) w,count(*) FILTER(WHERE delay>300) l FROM route_visits WHERE abs(delay)<=3600 GROUP BY g,day''')
        moments={g:(d,nn,ee,ww,ll,en,wn,ln) for g,d,nn,ee,ww,ll,en,wn,ln in db.execute('SELECT g,count(*),sum(n*n),sum(e*e),sum(w*w),sum(l*l),sum(e*n),sum(w*n),sum(l*n) FROM daily GROUP BY g').fetchall()}
        rows=db.execute('''SELECT g,count(*) n,count(DISTINCT day) d,min(day),max(day),quantile_cont(delay,[0.1,0.5,0.9]),
          count(*) FILTER(WHERE delay < -60),count(*) FILTER(WHERE delay BETWEEN -60 AND 300),count(*) FILTER(WHERE delay>300),count(*) FILTER(WHERE delay<0)
          FROM route_visits WHERE abs(delay)<=3600 GROUP BY g HAVING n>=30 AND d>=5''').fetchall()
        timing={}
        for g,n,d,lo,hi,q,e,w,l,b in rows:
            shares=[e/n,w/n,l/n];days,nn,ee,ww,ll,en,wn,ln=moments[g];intervals=[]
            for p,xx,xn in zip(shares,[ee,ww,ll],[en,wn,ln]):
                se=math.sqrt(max(0,days/(days-1)*(xx-2*p*xn+p*p*nn)/(n*n)))
                intervals.append([round(max(0,p-1.96*se),4),round(min(1,p+1.96*se),4)])
            timing[g]={'observations':n,'distinctDates':d,'coverageStart':str(lo),'coverageEnd':str(hi),'medianSeconds':q[1],'p10Seconds':q[0],'p90Seconds':q[2],
              'earlyShare':shares[0],'withinShare':shares[1],'lateShare':shares[2],'beforeScheduleShare':b/n,'shareIntervals':intervals,'nearbyFullBusReports':profiles[g].get('100m_180s',(0,0))[0]}
        risk={}
        for g,n,days,lo,hi in db.execute('SELECT g,count(*),count(DISTINCT day),min(day),max(day) FROM route_visits GROUP BY g').fetchall():
            level,reason=classify(n,days,profiles[g]);labels[level]+=1;reasons[reason]+=1
            if level!='unknown':risk[g]={'level':level,'recordedVisits':n,'distinctDates':days,'coverageStart':str(lo),'coverageEnd':str(hi),'profiles':{p:{'reportedVisits':profiles[g].get(p,(0,0))[0],'reportDates':profiles[g].get(p,(0,0))[1]} for p in POLICY['profiles']}}
        fn=quote(route,safe='')+'.json';timing_files[route]='reliability-routes/'+fn;risk_files[route]='passup-risk-routes/'+fn
        write(out/timing_files[route],{'groups':timing});write(out/risk_files[route],{'groups':risk});total_groups+=len(timing)
    common={'schemaVersion':2,'grouping':GROUPING,'groupWindowMinutes':WINDOW_MINUTES,'coverageStart':meta['coverageStart'],'coverageEnd':meta['coverageEnd'],'sources':meta['sources'],'routes':routes}
    timing={**common,'groups':{},'routeFiles':timing_files,'eligibleGroups':total_groups,'intervalMethod':'95% normal approximation with daily cluster-robust variance; descriptive, not forecast coverage','minObservations':30,'minDates':5}
    risk={**common,'groups':{},'routeFiles':risk_files,'policy':POLICY,'labelCounts':dict(labels),'reasonCounts':dict(reasons)}
    write(out/'reliability.json',timing);write(out/'passup-risk.json',risk)
    write(out/'reliability.report.json',{**meta,'eligibleGroups':total_groups,'intervalMethod':timing['intervalMethod'],'passupAudit':report_audit})
    write(out/'passup-risk.report.json',{k:v for k,v in risk.items() if k not in ['groups','routeFiles']})
    print(json.dumps({'coverage':[meta['coverageStart'],meta['coverageEnd']],'routes':len(routes),'timingGroups':total_groups,'labels':dict(labels),'passups':report_audit}),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--departures',nargs='+',required=True);p.add_argument('--passups',required=True);p.add_argument('--work',required=True);p.add_argument('--output',required=True);p.add_argument('--skip-import',action='store_true');p.add_argument('--resume-import',action='store_true');a=p.parse_args()
    work=Path(a.work);work.mkdir(parents=True,exist_ok=True);out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    if not a.skip_import:ingest(a.departures,work,a.resume_import)
    db=connect(work);meta=json.loads((work/'source-report.json').read_text());export(db,meta,a.passups,out,work);db.close()
