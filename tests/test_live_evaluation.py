from copy import deepcopy
from datetime import datetime, timezone
import unittest
from src.evaluate_live import match_events, select_at_horizon


class LiveEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.row={'route_number':'BLUE','stop_number':'11027','variant_name':'St. Norbert',
                  'scheduled_departure_raw':'2026-09-14T19:30:00','trip_key':'trip-a','scheduled_stop_key':'event-a',
                  'snapshot_id':'s1','received_at_utc':'2026-09-15T00:24:30+00:00','cancelled':False,
                  'estimated_departure_utc':'2026-09-15T00:31:00+00:00','estimated_delay_seconds':60}
        self.outcome={'route_number':'BLUE','stop_number':'11027','route_destination':'St. Norbert',
                      'scheduled_time':'2026-09-14T19:30:00.000','row_id':'measured-a','deviation':'-90'}
        self.scheduled=datetime(2026,9,15,0,30,tzinfo=timezone.utc)

    def test_exact_join_accepts_presentation_normalization_only(self):
        actual=deepcopy(self.outcome);actual['route_destination']=' ST.  NORBERT '
        self.assertEqual(match_events([self.row],[actual])[0]['status'],'matched')
        actual['scheduled_time']='2026-09-14T19:30:01'
        self.assertEqual(match_events([self.row],[actual])[0]['status'],'no_exact_outcome')
        actual=deepcopy(self.outcome);actual['route_destination']='Different destination'
        self.assertEqual(match_events([self.row],[actual])[0]['status'],'no_exact_outcome')

    def test_ambiguous_outcomes_and_live_identity_are_rejected(self):
        other=deepcopy(self.outcome);other['row_id']='measured-b'
        self.assertEqual(match_events([self.row],[self.outcome,other])[0]['status'],'multiple_outcomes')
        other=deepcopy(self.row);other['trip_key']='trip-b'
        self.assertEqual({m['status'] for m in match_events([self.row,other],[self.outcome])},
                         {'multiple_live_events_same_match_key'})

    def test_future_revision_never_changes_cutoff_prediction(self):
        future=deepcopy(self.row);future.update(snapshot_id='s2',received_at_utc='2026-09-15T00:25:01+00:00',estimated_delay_seconds=999)
        selected,reason=select_at_horizon([future,self.row],self.scheduled,5)
        self.assertIsNone(reason);self.assertEqual(selected['estimated_delay_seconds'],60)
        stale=deepcopy(self.row);stale['received_at_utc']='2026-09-15T00:23:29+00:00'
        self.assertEqual(select_at_horizon([stale],self.scheduled,5)[1],'snapshot_stale')

    def test_latest_cancellation_or_missing_estimate_does_not_fall_back(self):
        cancelled=deepcopy(self.row);cancelled.update(snapshot_id='s2',received_at_utc='2026-09-15T00:24:59+00:00',cancelled=True)
        self.assertEqual(select_at_horizon([self.row,cancelled],self.scheduled,5)[1],'cancelled_at_cutoff')
        cancelled.update(cancelled=False,estimated_departure_utc=None,estimated_delay_seconds=None)
        self.assertEqual(select_at_horizon([self.row,cancelled],self.scheduled,5)[1],'estimate_missing')


if __name__=='__main__':
    unittest.main()
