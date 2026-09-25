"""As-of estimate revisions and earlier-stop estimate proxies; never measured bus progress."""
from datetime import datetime, timedelta
import re
from .transit_api import LOCAL_ZONE, api_time


def revision_feature(rows, current, cutoff, lag_seconds=180, max_age_seconds=90):
    earlier_cutoff = cutoff - timedelta(seconds=lag_seconds)
    earlier = [r for r in rows if datetime.fromisoformat(r['received_at_utc']) <= earlier_cutoff]
    if not earlier:
        return None, 'revision_history_missing'
    old = max(earlier, key=lambda r:(r['received_at_utc'],r['snapshot_id']))
    old_received = datetime.fromisoformat(old['received_at_utc'])
    if (earlier_cutoff-old_received).total_seconds() > max_age_seconds:
        return None, 'revision_history_stale'
    if old.get('cancelled') or old.get('estimated_delay_seconds') is None:
        return None, 'revision_history_unavailable'
    if not current.get('bus_key') or old.get('bus_key') != current['bus_key']:
        return None, 'revision_bus_changed_or_missing'
    elapsed = (datetime.fromisoformat(current['received_at_utc'])-old_received).total_seconds()
    if elapsed <= 0:
        return None, 'revision_invalid_order'
    change = current['estimated_delay_seconds']-old['estimated_delay_seconds']
    return {'revision_seconds':change,'revision_interval_seconds':elapsed,
            'revision_seconds_per_minute':change/elapsed*60,
            'history_snapshot_id':old['snapshot_id'],'history_received_at_utc':old['received_at_utc']}, None


def clock_seconds(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,2}:\d{2}:\d{2}',value):
        raise ValueError('Expected clock-only HH:MM:SS.')
    hour,minute,second=map(int,value.split(':'))
    if not (0<=hour<48 and 0<=minute<60 and 0<=second<60):
        raise ValueError('Invalid clock time.')
    return hour*3600+minute*60+second


def resolve_trip_schedule(events, target_event_key, anchor_utc):
    """Anchor clock-only response order to an already-known dated target event.

    Midnight rollovers require a backwards jump of at least 12 hours. Smaller backwards
    jumps, duplicate target IDs, ambiguous DST times, and schedule mismatches are rejected.
    """
    positions=[i for i,event in enumerate(events) if str(event.get('key'))==target_event_key]
    if len(positions)!=1:
        raise ValueError('Target event not unique in trip response.')
    target=positions[0]; offsets=[]; day=0
    for event in events:
        value=clock_seconds(event.get('times',{}).get('departure',{}).get('scheduled'))
        candidate=value+day*86400
        if offsets and candidate<offsets[-1]:
            if offsets[-1]-candidate < 43200:
                raise ValueError('Non-monotonic trip schedule.')
            day+=1;candidate=value+day*86400
        offsets.append(candidate)
    local=anchor_utc.astimezone(LOCAL_ZONE).replace(tzinfo=None)
    if offsets[target]%86400 != local.hour*3600+local.minute*60+local.second:
        raise ValueError('Trip clock disagrees with dated stop anchor.')
    if offsets[-1]-offsets[0] > 43200:
        raise ValueError('Trip spans over twelve hours; date resolution refused.')
    resolved=[]
    for offset in offsets:
        wall=local+timedelta(seconds=offset-offsets[target])
        utc,issue=api_time(wall.isoformat())
        if issue:
            raise ValueError('Ambiguous trip date resolution.')
        resolved.append(utc)
    return target,resolved


def estimate_near_schedule(value, scheduled):
    seconds=clock_seconds(value)
    local=scheduled.astimezone(LOCAL_ZONE).replace(tzinfo=None)
    midnight=local.replace(hour=0,minute=0,second=0)
    candidates=[]
    for shift in [-1,0,1]:
        utc,issue=api_time((midnight+timedelta(days=shift,seconds=seconds)).isoformat())
        if not issue:
            candidates.append(utc)
    if not candidates:
        raise ValueError('Estimate date cannot be resolved.')
    candidates.sort(key=lambda d:abs((d-scheduled).total_seconds()))
    if len(candidates)>1 and abs((candidates[0]-scheduled).total_seconds())==abs((candidates[1]-scheduled).total_seconds()):
        raise ValueError('Estimate date is ambiguous.')
    if abs((candidates[0]-scheduled).total_seconds())>3600:
        raise ValueError('Estimate exceeds one-hour date-resolution bound.')
    return candidates[0]


def upstream_feature(trip_snapshots, current, cutoff, max_age_seconds=240):
    available=[s for s in trip_snapshots if s['resource_id']==current['trip_key'] and
               datetime.fromisoformat(s['received_at_utc'])<=cutoff]
    if not available:
        return None, 'trip_snapshot_missing'
    snapshot=max(available,key=lambda s:(s['received_at_utc'],s['snapshot_id']))
    received=datetime.fromisoformat(snapshot['received_at_utc'])
    if (cutoff-received).total_seconds()>max_age_seconds:
        return None,'trip_snapshot_stale'
    if snapshot['http_status']!=200 or snapshot['error_kind']:
        return None,'trip_response_error'
    trip=snapshot['payload'].get('trip',{})
    if str(trip.get('key')) != current['trip_key']:
        return None,'trip_identity_mismatch'
    if not current.get('bus_key') or str((trip.get('bus') or {}).get('key'))!=current['bus_key']:
        return None,'trip_bus_changed_or_missing'
    events=trip.get('scheduled-stops',[])
    try:
        target,scheduled=resolve_trip_schedule(events,current['scheduled_stop_key'],datetime.fromisoformat(current['scheduled_departure_utc']))
    except (ValueError,TypeError,KeyError):
        return None,'trip_date_unresolved'
    if str(events[target].get('cancelled','false')).lower() in ('true','1'):
        return None,'trip_target_cancelled'
    # The most recent earlier stop estimated to have been passed by snapshot receipt.
    # This remains an API estimate; it is not evidence that the bus actually passed it.
    for position in range(target-1,-1,-1):
        event=events[position]
        if str(event.get('cancelled','false')).lower() in ('true','1'):
            continue
        try:
            estimated=estimate_near_schedule(event.get('times',{}).get('departure',{}).get('estimated'),scheduled[position])
        except (ValueError,TypeError):
            continue
        age=(received-estimated).total_seconds()
        if scheduled[position] >= scheduled[target] or not 0<=age<=600:
            continue
        return {'upstream_delay_seconds':(estimated-scheduled[position]).total_seconds(),
                'upstream_snapshot_id':snapshot['snapshot_id'],'upstream_received_at_utc':snapshot['received_at_utc'],
                'upstream_event_key':str(event['key']),'upstream_stop_key':str(event.get('stop',{}).get('key')),
                'upstream_bus_key':current['bus_key'],'upstream_trip_key':current['trip_key'],
                'upstream_response_index':position,'target_response_index':target,
                'upstream_scheduled_departure_utc':scheduled[position].isoformat(),
                'upstream_estimated_departure_utc':estimated.isoformat(),
                'upstream_estimated_passage_age_seconds':age,
                'date_anchor_snapshot_id':current['snapshot_id'],
                'progress_measurement_kind':'earlier_stop_api_estimate_proxy'},None
    return None,'recent_earlier_stop_proxy_missing'
