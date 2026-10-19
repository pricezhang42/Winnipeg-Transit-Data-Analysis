"""Approved report-to-recorded-departure ratio categories, not probabilities."""
from collections import Counter

POLICY = {
    'version': 'report-scheduled-visit-ratio-v1',
    'profile': '150m',
    'minimumDepartures': 60,
    'minimumDepartureDates': 10,
    'zeroReportMinimumDepartures': 120,
    'mediumRatio': 0.01,
    'highRatio': 0.025,
    'mediumReportDates': 2,
    'highReportDates': 3,
}


def classify(group):
    """Use the broad report count; narrower profiles are audit information only."""
    reports, dates = group['reportCount'], group['reportDates']
    departures, departure_dates = group['recordedDepartures'], group['departureDates']
    if departures < POLICY['minimumDepartures'] or departure_dates < POLICY['minimumDepartureDates']:
        return 'unknown', 'insufficient_reference_history'
    # A report/visit ratio can exceed one because these are different units.
    # Retain the evidence but flag the anomaly rather than capping it.
    if reports > departures:
        return 'unknown', 'reports_exceed_recorded_visits'
    if reports == 0:
        if departures < POLICY['zeroReportMinimumDepartures']:
            return 'unknown', 'insufficient_zero_report_history'
        return 'low', 'report_to_recorded_visit_ratio'
    ratio = reports / departures
    if ratio >= POLICY['highRatio']:
        if dates < POLICY['highReportDates']:
            return 'unknown', 'insufficient_reporting_dates'
        return 'high', 'report_to_recorded_visit_ratio'
    if ratio >= POLICY['mediumRatio']:
        if dates < POLICY['mediumReportDates']:
            return 'unknown', 'insufficient_reporting_dates'
        return 'medium', 'report_to_recorded_visit_ratio'
    return 'low', 'report_to_recorded_visit_ratio'


def summarize_policy(groups):
    labels, reasons = Counter(), Counter()
    for group in groups.values():
        level, reason = classify(group)
        labels[level] += 1
        if level == 'unknown':
            reasons[reason] += 1
    return {
        'status': 'approved', 'policy': POLICY,
        'warning': 'Descriptive report-to-recorded-visit ratios, not boarding probabilities. Reports use report time; recorded visits use scheduled time.',
        'groups': len(groups),
        'groupsWithReports': sum(group['reportCount'] > 0 for group in groups.values()),
        'labelCounts': {level: labels[level] for level in ('low', 'medium', 'high', 'unknown')},
        'unknownReasons': dict(sorted(reasons.items())),
        'groupsWithReportsWithoutReferenceDepartures': sum(group['reportCount'] > 0 and group['recordedDepartures'] == 0 for group in groups.values()),
    }
