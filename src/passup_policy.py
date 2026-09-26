"""Versioned descriptive labels; scores are not probabilities."""
POLICY = {'version':'reported-pattern-v2','minVisits':60,'minDates':10,'zeroReportMinVisits':120,'scorePenaltyVisits':50,'mediumScore':0.01,'highScore':0.025,'mediumReportDates':2,'highReportDates':3,'profiles':['60m_120s','100m_180s','150m_300s']}

def category(visits, dates, report_visits, report_dates):
    if visits<60 or dates<10 or report_visits<0 or report_visits>visits:return 'unknown'
    if report_visits==0:return 'low' if visits>=120 else 'unknown'
    score=report_visits/(visits+50)
    if score>=.025:return 'high' if report_dates>=3 else 'unknown'
    if score>=.01:return 'medium' if report_dates>=2 else 'unknown'
    return 'low'

# v2: reports that cannot be matched to one stop/visit are left out of the counts
# and never force Unknown on nearby groups. Unstable stop coordinates still do.
def classify(visits, dates, profiles, unstable=False):
    if unstable:return 'unknown','unstable_stop_coordinates'
    labels=[category(visits,dates,*profiles.get(p,(0,0))) for p in POLICY['profiles']]
    if len(set(labels))!=1:return 'unknown','matching_sensitive'
    if labels[0]=='unknown':return 'unknown','insufficient_history_or_persistence'
    return labels[0],'stable_historical_pattern'
