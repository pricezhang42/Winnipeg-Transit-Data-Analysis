"""Nearest-only matching retains limits, route/date context and visit deduplication."""
import csv
import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

import duckdb

from build_history import match_reports, nearest_stop_candidates, nearest_visits, stops_on_report_days


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect()
        self.addCleanup(self.db.close)
        self.day = datetime(2026, 9, 25).date()
        self.db.execute('''CREATE TABLE grouped_visits(
          vid BIGINT, route VARCHAR, dest VARCHAR, day DATE, stop VARCHAR,
          lon DOUBLE, lat DOUBLE, g VARCHAR, actual TIMESTAMP, actual_day DATE)''')
        self.group = '["F8","A","downtown","weekday",12]'
        # A's coordinates span >30 m, but average exactly to the report position.
        # B is only about 7 m away: it must not disqualify A as a runner-up.
        self.db.executemany('INSERT INTO grouped_visits VALUES (?,?,?,?,?,?,?,?,?,?)', [
            (1, 'F8', 'downtown', self.day, 'A', -97.001, 49, self.group, '2026-09-25T13:00:00', self.day),
            (2, 'F8', 'downtown', self.day, 'A', -96.999, 49, self.group, '2026-09-25T13:00:20', self.day),
            (3, 'F8', 'downtown', self.day, 'B', -97.0001, 49, 'other', '2026-09-25T13:00:09', self.day),
        ])

    def candidate(self, report='r', time='2026-09-25T13:00:09', stop='A'):
        when = datetime.fromisoformat(time)
        return (report, 'F8', 'downtown', stop, when, when.date(), 0)

    def test_spread_and_close_runner_ups_do_not_disqualify_nearest(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / 'reports.csv'
            row = {'Pass-Up ID': 'r1', 'Pass-Up Type': 'Full Bus Pass-Up',
                   'Time': '09/25/2026 01:00:09 PM', 'Route Number': 'F8',
                   'Route Destination': 'Downtown', 'Location': 'POINT (-97 49)'}
            with source.open('w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
                writer.writerows([row, {**row, 'Pass-Up ID': 'r2'}])
            groups, audit = match_reports(self.db, source, {'dailyCoverage': [{'date': str(self.day)}]}, work)
            stops = stops_on_report_days(self.db)
            self.assertEqual(set(stops[('F8', 'downtown', self.day)]), {'A', 'B'})
            for profile in ['60m_120s', '100m_180s', '150m_300s']:
                self.assertEqual(groups[self.group][profile], (1, 1))
                self.assertEqual(audit[profile + 'Reports'], 2)
            matches = json.loads((work / 'matches.json').read_text())
            self.assertEqual({m['visit'] for m in matches}, {1})
            self.assertNotIn('noNearbyStop', audit)
            self.assertNotIn('noTimedVisit', audit)

    def test_nearest_stop_limits_context_and_ties(self):
        when = datetime(2026, 9, 25, 13)
        reports = [('r', 'F8', 'downtown', when, self.day, -97, 49)]
        context = ('F8', 'downtown', self.day)
        audit = Counter()
        stops = {context: {'B': (-97, 49), 'A': (-97, 49)}}
        self.assertEqual(nearest_stop_candidates(reports, stops, audit)[0][3], 'A')
        for stops in [{}, {context: {'far': (-98, 49)}}, {('F8', 'other', self.day): {'A': (-97, 49)}}]:
            self.assertEqual(nearest_stop_candidates(reports, stops, audit), [])
        self.assertEqual(audit['noNearbyStop'], 3)

    def test_nearest_visit_ties_limits_and_missing_stop(self):
        candidates = [self.candidate('tie', '2026-09-25T13:00:10'),
                      self.candidate('limit', '2026-09-25T12:55:00'),
                      self.candidate('outside', '2026-09-25T12:54:59'),
                      self.candidate('missing', stop='missing')]
        nearest = nearest_visits(self.db, candidates)
        self.assertEqual(set(nearest), {'tie', 'limit'})
        self.assertEqual(nearest['tie'][0], 1)
        self.assertEqual(nearest['limit'][3], 300)

    def test_visit_matching_crosses_midnight(self):
        self.db.execute("UPDATE grouped_visits SET actual='2026-09-25T23:59:59' WHERE vid=1")
        nearest = nearest_visits(self.db, [self.candidate(time='2026-09-26T00:00:01')])
        self.assertEqual(nearest['r'][0], 1)
        self.assertEqual(nearest['r'][3], 2)

    def test_no_covered_reports_is_an_empty_match(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / 'empty.csv'
            source.write_text('Pass-Up ID,Pass-Up Type,Time,Route Number,Route Destination,Location\n')
            groups, audit = match_reports(self.db, source, {'dailyCoverage': []}, work)
            self.assertEqual(groups, {})
            self.assertEqual(audit.get('sourceReports', 0), 0)
            self.assertEqual(json.loads((work / 'matches.json').read_text()), [])


if __name__ == '__main__':
    unittest.main()
