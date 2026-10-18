"""Compare unapproved report-label rules; never assign production risk labels."""
from collections import Counter
from statistics import median

PROFILES = ('60m', '100m', '150m')
CANDIDATES = {
    'legacy_score_adapted_to_reports': {
        'description': 'Comparison only: reports/(recorded departures+50), with legacy score boundaries.',
        'minimumDepartures': 60, 'minimumDepartureDates': 10, 'zeroReportMinimumDepartures': 120,
        'scorePenalty': 50, 'mediumScore': .01, 'highScore': .025,
        'mediumReportDates': 2, 'highReportDates': 3,
    },
    'report_count_and_persistence': {
        'description': 'Comparison only: report counts and reporting-date persistence, with exposure coverage checks.',
        'minimumDepartures': 60, 'minimumDepartureDates': 10, 'zeroReportMinimumDepartures': 120,
        'mediumReports': 2, 'mediumReportDates': 2, 'highReports': 5, 'highReportDates': 3,
    },
}


def candidate_level(group, profile, candidate):
    """Experimental rules; reports may exceed departure counts and are not visits."""
    rule = CANDIDATES[candidate]
    departures = group['recordedDepartures']
    if departures < rule['minimumDepartures'] or group['departureDates'] < rule['minimumDepartureDates']:
        return 'unknown'
    evidence = group['profiles'][profile]
    reports, dates = evidence['reports'], evidence['reportDates']
    if reports == 0:
        return 'low' if departures >= rule['zeroReportMinimumDepartures'] else 'unknown'
    if candidate == 'legacy_score_adapted_to_reports':
        score = reports / (departures + rule['scorePenalty'])
        if score >= rule['highScore']:
            return 'high' if dates >= rule['highReportDates'] else 'unknown'
        if score >= rule['mediumScore']:
            return 'medium' if dates >= rule['mediumReportDates'] else 'unknown'
    else:
        if reports >= rule['highReports']:
            return 'high' if dates >= rule['highReportDates'] else 'unknown'
        if reports >= rule['mediumReports']:
            return 'medium' if dates >= rule['mediumReportDates'] else 'unknown'
    return 'low'


def summarize_candidates(groups):
    comparisons = {}
    for candidate, rule in CANDIDATES.items():
        counts = Counter()
        for group in groups.values():
            levels = {candidate_level(group, profile, candidate) for profile in PROFILES}
            counts[levels.pop() if len(levels) == 1 else 'unknown'] += 1
        comparisons[candidate] = {'rules': rule, 'labelCounts': {level: counts[level] for level in ('low', 'medium', 'high', 'unknown')}}
    positive = [g['reportCount'] for g in groups.values() if g['reportCount'] > 0]
    return {
        'status': 'pending_policy_review', 'productionLabelsAssigned': False,
        'warning': 'Experimental descriptions, not probabilities. Multiple reports can describe one bus; report and departure time bases differ.',
        'groups': len(groups), 'groupsWithReports': len(positive),
        'groupsWithReportsWithoutReferenceDepartures': sum(g['reportCount'] > 0 and g['recordedDepartures'] == 0 for g in groups.values()),
        'reportCountDistributionAmongPositiveGroups': {'minimum': min(positive) if positive else None,
            'median': median(positive) if positive else None, 'maximum': max(positive) if positive else None},
        'candidates': comparisons,
    }
