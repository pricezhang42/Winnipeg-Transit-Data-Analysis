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
            risk=next(iter(json.loads((out/'passup-risk-routes/F8.json').read_text())['groups'].values()))
            self.assertEqual(risk['level'],'low');self.assertEqual(risk['profiles']['60m_120s']['reportedVisits'],1)
            db.close()
if __name__=='__main__':unittest.main()
