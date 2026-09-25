from copy import deepcopy
from datetime import datetime,timezone
import unittest

from src.live_signals import resolve_trip_schedule,estimate_near_schedule,revision_feature,upstream_feature

UTC=timezone.utc


def event(key,scheduled,estimated=None):
    return {'key':key,'stop':{'key':key},'cancelled':'false',
            'times':{'departure':{'scheduled':scheduled,'estimated':estimated or scheduled}}}


class LiveSignalTests(unittest.TestCase):
    def setUp(self):
        self.cutoff=datetime(2026,9,15,0,25,tzinfo=UTC)
        self.current={'trip_key':'trip-a','scheduled_stop_key':'target','bus_key':'bus-a',
                      'scheduled_departure_utc':'2026-09-15T00:30:00+00:00',
                      'snapshot_id':'current','received_at_utc':'2026-09-15T00:24:30+00:00',
                      'estimated_delay_seconds':60,'cancelled':False}
        self.trip={'resource_id':'trip-a','snapshot_id':'trip-snapshot',
                   'received_at_utc':'2026-09-15T00:24:00+00:00','http_status':200,'error_kind':None,
                   'payload':{'trip':{'key':'trip-a','bus':{'key':'bus-a'},'scheduled-stops':[
                       event('previous','19:22:00','19:23:00'),event('target','19:30:00','19:31:00')]}}}

    def test_revision_ignores_future_and_requires_stable_bus(self):
        old={**self.current,'snapshot_id':'old','received_at_utc':'2026-09-15T00:21:30+00:00','estimated_delay_seconds':30}
        future={**old,'snapshot_id':'future','received_at_utc':'2026-09-15T00:22:01+00:00','estimated_delay_seconds':999}
        signal,reason=revision_feature([old,future],self.current,self.cutoff)
        self.assertIsNone(reason);self.assertEqual(signal['revision_seconds'],30)
        self.assertEqual(signal['revision_seconds_per_minute'],10)
        old['bus_key']='other'
        self.assertEqual(revision_feature([old],self.current,self.cutoff)[1],'revision_bus_changed_or_missing')

    def test_upstream_uses_matching_bus_trip_and_prior_response_order(self):
        feature,reason=upstream_feature([self.trip],self.current,self.cutoff)
        self.assertIsNone(reason);self.assertEqual(feature['upstream_delay_seconds'],60)
        self.assertEqual(feature['upstream_event_key'],'previous')
        self.assertLess(feature['upstream_response_index'],feature['target_response_index'])
        changed=deepcopy(self.trip);changed['payload']['trip']['bus']['key']='different-bus'
        self.assertEqual(upstream_feature([changed],self.current,self.cutoff)[1],'trip_bus_changed_or_missing')
        changed=deepcopy(self.trip);changed['payload']['trip']['key']='different-trip'
        self.assertEqual(upstream_feature([changed],self.current,self.cutoff)[1],'trip_identity_mismatch')

    def test_future_or_stale_trip_snapshot_cannot_supply_signal(self):
        future=deepcopy(self.trip);future.update(snapshot_id='future',received_at_utc='2026-09-15T00:25:01+00:00')
        future['payload']['trip']['scheduled-stops'][0]['times']['departure']['estimated']='19:15:00'
        signal,reason=upstream_feature([self.trip,future],self.current,self.cutoff)
        self.assertIsNone(reason);self.assertEqual(signal['upstream_snapshot_id'],'trip-snapshot')
        stale=deepcopy(self.trip);stale['received_at_utc']='2026-09-15T00:20:59+00:00'
        self.assertEqual(upstream_feature([stale],self.current,self.cutoff)[1],'trip_snapshot_stale')

    def test_future_passage_and_cancelled_earlier_stop_are_unavailable(self):
        trip=deepcopy(self.trip)
        trip['payload']['trip']['scheduled-stops'][0]['times']['departure']['estimated']='19:25:00'
        self.assertEqual(upstream_feature([trip],self.current,self.cutoff)[1],'recent_earlier_stop_proxy_missing')
        trip=deepcopy(self.trip);trip['payload']['trip']['scheduled-stops'][0]['cancelled']='true'
        self.assertEqual(upstream_feature([trip],self.current,self.cutoff)[1],'recent_earlier_stop_proxy_missing')

    def test_midnight_resolves_from_dated_anchor(self):
        events=[event('before','23:58:00'),event('target','00:05:00')]
        anchor=datetime(2026,9,15,5,5,tzinfo=UTC)
        index,times=resolve_trip_schedule(events,'target',anchor)
        self.assertEqual(index,1)
        self.assertEqual(times[0],datetime(2026,9,15,4,58,tzinfo=UTC))
        self.assertEqual(estimate_near_schedule('00:01:00',times[0]),datetime(2026,9,15,5,1,tzinfo=UTC))

    def test_inconsistent_clock_and_dst_ambiguity_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_trip_schedule([event('prior','19:31:00'),event('target','19:30:00')],'target',datetime(2026,9,15,0,30,tzinfo=UTC))
        with self.assertRaises(ValueError):
            resolve_trip_schedule([event('target','19:29:00')],'target',datetime(2026,9,15,0,30,tzinfo=UTC))
        with self.assertRaises(ValueError):
            resolve_trip_schedule([event('prior','01:30:00'),event('target','02:05:00')],'target',datetime(2026,11,1,8,5,tzinfo=UTC))


if __name__=='__main__':
    unittest.main()
