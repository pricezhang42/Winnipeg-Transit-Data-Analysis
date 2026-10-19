"""Report-only grouping never requires an actual departure or a visit identity."""
import csv
import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

import duckdb
from build_history import match_reports, nearest_stop_candidates, report_evidence


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect()
        self.addCleanup(self.db.close)
        self.day = datetime(2026, 9, 25).date()
        # Deliberately omit actual departure timestamps and visit IDs.
        self.db.execute('''CREATE TABLE grouped_visits(
          route VARCHAR, dest VARCHAR, day DATE, stop VARCHAR, lon DOUBLE, lat DOUBLE)''')
        self.db.executemany('INSERT INTO grouped_visits VALUES (?,?,?,?,?,?)', [
            ('F8', 'downtown', self.day, 'A', -97.001, 49),
            ('F8', 'downtown', self.day, 'A', -96.999, 49),
            ('F8', 'downtown', self.day, 'B', -97.0001, 49),
            ('F8', 'downtown', '2026-09-26', 'A', -97, 49),
        ])
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.work = Path(self.folder.name)

    def match(self, entries):
        source = self.work / 'reports.csv'
        base = {'Pass-Up ID': 'r', 'Pass-Up Type': 'Full Bus Pass-Up',
                'Time': '09/25/2026 01:00:09 PM', 'Route Number': 'F8',
                'Route Destination': 'Downtown', 'Location': 'POINT (-97 49)'}
        with source.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(base))
            writer.writeheader()
            writer.writerows({**base, **entry} for entry in entries)
        return match_reports(self.db, source, {'dailyCoverage': [{'date': '2026-09-25'}, {'date': '2026-09-26'}]}, self.work)

    def test_reports_count_independently_and_duplicate_ids_do_not(self):
        groups, audit = self.match([{'Pass-Up ID': 'r1'}, {'Pass-Up ID': 'r2'}, {'Pass-Up ID': 'r1'}])
        group = groups['["F8","A","downtown","weekday",12]']
        for name in ('60m', '100m', '150m'):
            self.assertEqual(group['profiles'][name], {'reports': 2, 'reportDates': 1})
        self.assertEqual(audit['matchedReports'], 2)
        self.assertEqual(audit['duplicateReports'], 1)
        matches = json.loads((self.work / 'matches.json').read_text())
        self.assertTrue(all('visit' not in row and 'timeSeconds' not in row for row in matches))
        self.assertEqual({row['stop'] for row in matches}, {'A'})
        self.assertEqual(group['coverageStart'], '2026-09-25')

    def test_report_timestamp_controls_window_date_and_weekend(self):
        groups, audit = self.match([
            {'Pass-Up ID': 'before', 'Time': '09/25/2026 01:59:59 PM'},
            {'Pass-Up ID': 'after', 'Time': '09/25/2026 02:00:00 PM'},
            {'Pass-Up ID': 'midnight-before', 'Time': '09/25/2026 11:59:59 PM'},
            {'Pass-Up ID': 'midnight-after', 'Time': '09/26/2026 12:00:00 AM'},
        ])
        contexts = {(json.loads(g)[3], json.loads(g)[4]) for g in groups}
        self.assertEqual(contexts, {('weekday', 12), ('weekday', 14), ('weekday', 22), ('weekend', 0)})
        self.assertEqual(audit['matchedReports'], 4)

    def test_spatial_profiles_and_outer_limit_remain(self):
        groups, audit = self.match([
            {'Pass-Up ID': '70m', 'Location': 'POINT (-97 49.00063)'},
            {'Pass-Up ID': 'far', 'Location': 'POINT (-97 49.002)'},
        ])
        group = next(iter(groups.values()))
        self.assertNotIn('60m', group['profiles'])
        self.assertEqual(group['profiles']['100m']['reports'], 1)
        self.assertEqual(audit['noNearbyStop'], 1)
        self.assertEqual(audit['matchedReports'], 1)

    def test_report_group_without_departures_is_preserved(self):
        reports, _ = self.match([{'Time': '09/25/2026 02:00:00 PM'}])
        self.db.execute('CREATE TABLE route_visits(g VARCHAR, day DATE)')
        self.db.execute("INSERT INTO route_visits VALUES ('[\"F8\",\"A\",\"downtown\",\"weekday\",12]', '2026-09-25')")
        groups = report_evidence(self.db, reports, 'F8')
        reported = groups['["F8","A","downtown","weekday",14]']
        self.assertEqual(reported['reportCount'], 1)
        self.assertEqual(reported['recordedDepartures'], 0)
        scheduled = groups['["F8","A","downtown","weekday",12]']
        self.assertEqual(scheduled['reportCount'], 0)
        self.assertEqual(scheduled['recordedDepartures'], 1)

    def test_nearest_stop_missing_context_and_ties(self):
        when = datetime(2026, 9, 25, 13)
        reports = [('r', 'F8', 'downtown', when, self.day, -97, 49)]
        context = ('F8', 'downtown', self.day)
        audit = Counter()
        self.assertEqual(nearest_stop_candidates(reports, {context: {'B': (-97, 49), 'A': (-97, 49)}}, audit)[0][3], 'A')
        self.assertEqual(nearest_stop_candidates(reports, {}, audit), [])
        self.assertEqual(audit['noNearbyStop'], 1)

    def test_empty_and_out_of_scope_reports(self):
        groups, audit = self.match([])
        self.assertEqual(groups, {})
        self.assertEqual(audit['matchedReports'], 0)
        groups, audit = self.match([
            {'Pass-Up ID': 'other', 'Pass-Up Type': 'Wheelchair User Pass-Up'},
            {'Pass-Up ID': 'old', 'Time': '09/24/2026 01:00:00 PM'},
            {'Pass-Up ID': 'bad', 'Location': 'invalid'},
        ])
        self.assertEqual(groups, {})
        self.assertEqual(audit['nonFullBusReports'], 1)
        self.assertEqual(audit['outsideCoveredDates'], 1)
        self.assertEqual(audit['invalidCoordinates'], 1)


if __name__ == '__main__':
    unittest.main()
