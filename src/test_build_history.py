import unittest,tempfile,csv,zipfile,json
from pathlib import Path
from datetime import datetime
from build_history import ingest,connect,export,key
class HistoryTests(unittest.TestCase):
    def test_two_hour_bins(self):
        at=lambda h,m:json.loads(key('F8','1','downtown',datetime(2026,9,25,h,m)))[-1]
        self.assertEqual([at(16,0),at(16,40),at(17,59),at(18,0),at(0,30),at(23,59)],[16,16,16,18,0,22])
    def test_archives_overlap_cutoff_and_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);work=root/'work';work.mkdir();out=root/'out';out.mkdir()
            rows=[]
            for day in [2,3,4,7,8,9,10,11,14,15]:
                for minute in [0,5,10,15,20,25,30,35,40,45,50,55]:
                    rows.append({'Row ID':f'{day}-{minute}','Stop Number':'1','Route Number':'F8','Route Destination':'Downtown','Day Type':'Weekday','Scheduled Time':f'2025-07-{day:02}T13:{minute:02}:00','Deviation':'-120','Location':'POINT (-97 49)'})
            for day in [5,6,7,8,9,12]:
                for minute in [0,5,10,15,20,25]:
                    rows.append({**rows[0],'Row ID':f'w-{day}-{minute}','Scheduled Time':f'2026-01-{day:02}T13:{minute:02}:00'})
            rows.append({**rows[0],'Row ID':'pre','Scheduled Time':'2025-06-28T13:00:00'})
            rows.extend([{**rows[0],'Row ID':'conflict-id','Scheduled Time':'2025-07-02T14:00:00','Deviation':d} for d in ['-120','-240']])
            rows.extend([{**rows[0],'Row ID':rid,'Scheduled Time':'2025-07-02T14:05:00','Deviation':d} for rid,d in [('natural-a','-120'),('natural-b','-240')]])
            rows.append({**rows[0],'Row ID':'duplicate-natural'})
            csvpath=root/'archive.csv'
            with csvpath.open('w') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            zippath=root/'archive.zip'
            with zipfile.ZipFile(zippath,'w') as z:z.write(csvpath,'archive.csv')
            # A second format with overlapping identities must not double count.
            with (root/'recent.csv').open('w') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerow({**rows[0],'Scheduled Time':'2025 Jul 02 01:00:00 PM'})
            ingest([zippath,root/'recent.csv'],work)
            audit=json.loads((work/'source-report.json').read_text())['audit']
            self.assertEqual(audit['conflictingSourceIds'],1)
            self.assertEqual(audit['conflictingVisitIdentities'],1)
            self.assertEqual(audit['collapsedConsistentCopies'],1)
            db=connect(work)
            self.assertEqual(db.execute('select count(*) from visits').fetchone()[0],156)
            columns={row[0] for row in db.execute('describe visits').fetchall()}
            self.assertTrue(columns.isdisjoint({'vid', 'actual', 'actual_day'}))
            self.assertEqual(json.loads(db.execute('select g from visits limit 1').fetchone()[0]),['F8','1','downtown','weekday',12])
            report={'Pass-Up ID':'x','Pass-Up Type':'Full Bus Pass-Up','Time':'07/02/2025 01:02:00 PM','Route Number':'F8','Route Destination':'Downtown','Location':'POINT (-97 49)'}
            with (root/'passups.csv').open('w') as f:
                w=csv.DictWriter(f,fieldnames=list(report));w.writeheader();w.writerow(report)
            export(db,json.loads((work/'source-report.json').read_text()),root/'passups.csv',out,work)
            groups=json.loads((out/'reliability-routes/F8.json').read_text())['groups']
            group=groups['["F8","1","downtown","weekday",12]']
            self.assertEqual(len(groups),1)
            self.assertEqual(group['medianSeconds'],120);self.assertEqual(group['observations'],156)
            self.assertEqual(group['distinctDates'],16)
            report_group=next(iter(json.loads((out/'passup-report-routes/F8.json').read_text())['groups'].values()))
            self.assertEqual(report_group['reportCount'],1)
            self.assertEqual(report_group['profiles']['60m']['reports'],1)
            self.assertNotIn('level', report_group)
            manifest=json.loads((out/'passup-report-counts.json').read_text())
            self.assertEqual(manifest['evidenceUnit'],'unique-report-id')
            self.assertEqual(manifest['labelStatus'],'pending_policy_review')
            self.assertFalse((out/'passup-risk.json').exists())
            db.close()

    def test_timing_boundaries_and_evidence_gates(self):
        import duckdb
        from build_history import timing_groups

        with duckdb.connect() as db:
            db.execute('CREATE TABLE route_visits(g VARCHAR, day DATE, delay BIGINT)')
            # Both +/-3600 are retained; +/-3601 are excluded. Exactly -60 and
            # +300 are within the on-time window, while -1 is before schedule.
            delays = [-3601, -3600, -61, -60, -1, 0, 300, 301, 3600, 3601]
            rows = [('eligible', f'2026-09-{day:02}', delay)
                    for day in range(1, 7) for delay in delays]
            rows += [('minimum', f'2026-09-{day:02}', 0)
                     for day in range(1, 6) for _ in range(6)]
            rows += [('too_few_dates', f'2026-09-{day:02}', 0)
                     for day in range(1, 5) for _ in range(10)]
            rows += [('too_few_visits', f'2026-09-{day % 5 + 1:02}', 0)
                     for day in range(29)]
            db.executemany('INSERT INTO route_visits VALUES (?, ?, ?)', rows)
            result = timing_groups(db, {'eligible': {'profiles': {'100m': {'reports': 2, 'reportDates': 1}}}})
            self.assertEqual(set(result), {'eligible', 'minimum'})
            self.assertEqual(result['minimum']['observations'], 30)
            self.assertEqual(result['minimum']['distinctDates'], 5)
            group = result['eligible']
            self.assertEqual(group['observations'], 48)
            self.assertEqual(group['distinctDates'], 6)
            self.assertEqual(group['medianSeconds'], -0.5)
            self.assertEqual([group[name] for name in ('earlyShare', 'withinShare', 'lateShare')], [.25, .5, .25])
            self.assertEqual(group['beforeScheduleShare'], .5)
            self.assertEqual(group['shareIntervals'], [[.25, .25], [.5, .5], [.25, .25]])
            self.assertEqual(group['nearbyFullBusReports'], 2)
            self.assertEqual(timing_groups(db, {})['eligible']['nearbyFullBusReports'], 0)
            db.execute('DELETE FROM route_visits')
            self.assertEqual(timing_groups(db, {}), {})

    def test_share_intervals_use_daily_clusters(self):
        import math
        from build_history import share_intervals

        # Unequal daily volumes and different outcomes expose weighting errors.
        visits = [4, 8, 3, 6, 9]
        early = [1, 0, 2, 1, 6]
        within = [2, 8, 0, 4, 2]
        late = [1, 0, 1, 1, 1]
        summary = {'dates': len(visits), 'visits': sum(visits),
                   'visit_squares': sum(n*n for n in visits)}
        shares, expected = [], []
        for name, counts in [('early', early), ('within', within), ('late', late)]:
            share = sum(counts) / sum(visits)
            shares.append(share)
            summary[name + '_squares'] = sum(n*n for n in counts)
            summary[name + '_cross'] = sum(a*b for a, b in zip(counts, visits))
            residuals = sum((x - share*n)**2 for x, n in zip(counts, visits))
            margin = 1.96 * math.sqrt(5/4 * residuals / sum(visits)**2)
            expected.append([round(max(0, share-margin), 4), round(min(1, share+margin), 4)])
        self.assertEqual(share_intervals(shares, summary), expected)

if __name__=='__main__':unittest.main()
