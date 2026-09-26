import unittest, tempfile, csv, io
from pathlib import Path
from contextlib import redirect_stdout
from audit_passups import choose_stop, choose_visit, build

class AuditTests(unittest.TestCase):
    def test_conservative_matching(self):
        self.assertEqual(choose_stop((-97,49),{'1':(-97,49)})[0],'1')
        self.assertEqual(choose_stop((-97,49),{'1':(-97,49),'2':(-97.00001,49)})[1],'ambiguous_stop')
        self.assertEqual(choose_stop((-97,49),{'1':(-98,49)})[1],'no_nearby_stop')
        self.assertEqual(choose_visit(1000,[(1,1010),(2,1020)])[1],'ambiguous_visit')
        self.assertEqual(choose_visit(1000,[(1,1010),(2,1200)])[0],1)
        self.assertEqual(choose_visit(1000,[(1,1400)])[1],'no_nearby_visit')
    def test_denominator_dedup_time_sign_and_wrong_direction(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            departure={'Row ID':'1','Stop Number':'1','Route Number':'F6','Route Destination':'Downtown','Scheduled Time':'2026 Sep 25 01:00:00 PM','Day Type':'Weekday','Deviation':'-120','Location':'POINT(-97 49)'}
            report={'Pass-Up ID':'a','Pass-Up Type':'Full Bus Pass-Up','Time':'09/25/2026 01:02:00 PM','Route Number':'F6','Route Destination':'Downtown','Location':'POINT(-97 49)'}
            for filename,rows in [('departures.csv',[departure,{**departure,'Row ID':'2'},{**departure,'Row ID':'3','Scheduled Time':'2026 Sep 25 02:00:00 PM'}]),('reports.csv',[report,{**report,'Pass-Up ID':'b'},{**report,'Pass-Up ID':'c','Route Destination':'Other'}])]:
                with (root/filename).open('w',newline='') as f:
                    writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            with redirect_stdout(io.StringIO()):result=build(root/'departures.csv',root/'reports.csv',root/'out')
            self.assertEqual(result['audit']['uniqueRecordedVisits'],2)
            self.assertEqual(result['audit']['duplicateVisitRows'],1)
            self.assertEqual(result['audit']['matched'],1)
            self.assertEqual(result['audit']['duplicate_report_for_visit'],1)
            self.assertEqual(result['audit']['no_nearby_stop'],1)
            self.assertEqual(result['groupsWithMatchedReports'][0]['recordedVisits'],1)
            self.assertFalse(result['groupsWithMatchedReports'][0]['publishable'])
if __name__=='__main__':unittest.main()
