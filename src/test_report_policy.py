import unittest
from report_policy import classify, summarize_policy


def group(reports=0, dates=0, visits=200, visit_dates=10):
    return {'reportCount': reports, 'reportDates': dates,
            'recordedDepartures': visits, 'departureDates': visit_dates}


class ReportPolicyTests(unittest.TestCase):
    def test_ratio_boundaries_and_persistence(self):
        cases = [(1, 1, 200, 'low'), (2, 2, 200, 'medium'),
                 (4, 2, 200, 'medium'), (5, 3, 200, 'high'),
                 (6, 3, 200, 'high'), (6, 1, 200, 'unknown'),
                 (2, 1, 200, 'unknown'), (5, 2, 200, 'unknown')]
        for reports, dates, visits, expected in cases:
            with self.subTest(reports=reports, dates=dates):
                self.assertEqual(classify(group(reports, dates, visits))[0], expected)

    def test_reference_minimums_and_zero_reports(self):
        self.assertEqual(classify(group(1, 1, 59))[0], 'unknown')
        self.assertEqual(classify(group(1, 1, 100, 9))[0], 'unknown')
        self.assertEqual(classify(group(2, 2, 60))[0], 'unknown')
        self.assertEqual(classify(group(3, 3, 60))[0], 'high')
        self.assertEqual(classify(group(visits=119))[0], 'unknown')
        self.assertEqual(classify(group(visits=120))[0], 'low')
        self.assertEqual(classify(group(1, 1, 0))[0], 'unknown')
        self.assertEqual(classify(group(201, 10, 200))[1], 'reports_exceed_recorded_visits')

    def test_narrow_profiles_do_not_change_categories(self):
        evidence = group(6, 3)
        evidence['profiles'] = {'60m': {'reports': 0, 'reportDates': 0}}
        self.assertEqual(classify(evidence)[0], 'high')
        report = summarize_policy({'a': evidence, 'b': group()})
        self.assertEqual(report['labelCounts'], {'low': 1, 'medium': 0, 'high': 1, 'unknown': 0})
        self.assertEqual(report['status'], 'approved')


if __name__ == '__main__':
    unittest.main()
