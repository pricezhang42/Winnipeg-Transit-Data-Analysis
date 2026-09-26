import unittest
import csv, tempfile, json, io
from pathlib import Path
from contextlib import redirect_stdout
from datetime import datetime
from export_reliability import key, summary, add, point, distance, destination, build
class ExportTests(unittest.TestCase):
    def test_sign_thresholds_and_dates(self):
        groups={}
        for i, delay in enumerate([-61,-60,0,300,301]):
            add(groups,'x',delay,f'2026-09-{i+1:02}')
        result=summary(groups['x']['values'],groups['x']['days'])
        self.assertEqual((result['earlyShare'],result['withinShare'],result['lateShare']),(.2,.6,.2))
        self.assertEqual(result['beforeScheduleShare'],.4)
        self.assertEqual(result['medianSeconds'],0)
        self.assertEqual(len(result['shareIntervals']),3)
    def test_csv_export_sign_dedup_and_report_matching(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);otp=folder/'otp.csv';reports=folder/'passups.csv'
            rows=[]
            for day in [1,2,3,4,8,9,10,11,14,15]:
                for i, deviation in enumerate([120,60,0,-300,-600]):
                    rows.append({'Row ID':f'{day}-{i}','Stop Number':'1','Route Number':'BLUE','Route Destination':'Downtown','Day Type':'Weekday','Scheduled Time':f'2026 Sep {day:02} 01:00:00 PM','Deviation':str(deviation),'Location':'POINT(-97 49)'})
            rows.append(rows[0])
            rows += [{**row,'Row ID':'F6-'+row['Row ID'],'Route Number':'F6'} for row in rows[:-1]]
            with otp.open('w') as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
            report={'Pass-Up ID':'1','Pass-Up Type':'Full Bus Pass-Up','Time':'09/01/2026 01:00:00 PM','Route Number':'BLUE','Route Destination':'Downtown','Location':'POINT(-97 49)'}
            with reports.open('w') as f:
                writer=csv.DictWriter(f,fieldnames=list(report));writer.writeheader();writer.writerows([report,report,{**report,'Pass-Up ID':'2','Route Destination':'Other direction'}])
            with redirect_stdout(io.StringIO()):
                result=build(otp,reports,folder/'result.json',None)
            self.assertEqual(result['routes'],['BLUE','F6'])
            self.assertEqual(len(result['groups']),2)
            stats=result['groups'][key('BLUE','1','Downtown',datetime(2026,9,1,13))]
            self.assertEqual(stats['observations'],50)
            self.assertEqual(stats['earlyShare'],.2)
            self.assertEqual(stats['lateShare'],.2)
            self.assertEqual(stats['nearbyFullBusReports'],1)
            self.assertEqual(result['audit']['duplicateIds'],1)
            self.assertEqual(result['passUpAudit']['duplicateIds'],1)
            self.assertEqual(result['passUpAudit']['noNearbyCompatibleStop'],1)

    def test_identity_and_locations(self):
        self.assertIn('"weekend","winter",0',key('BLUE','1','To Downtown',datetime(2026,12,5)))
        self.assertEqual(destination('To Downtown'),'downtown')
        self.assertIsNone(point('POINT(nan 49)'))
        self.assertEqual(distance((-97,49),(-97,49)),0)
if __name__=='__main__':unittest.main()
