import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from src.collect import Store, collector_lock, replay, status
from src.transit_api import ApiClient, AuthenticationError, api_time, normalize, sanitize

FIXTURE = Path(__file__).parent / 'fixtures/transit_synthetic.json'


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.bundle = json.loads(FIXTURE.read_text())
        self.item = self.bundle['responses'][0]

    def test_estimates_identity_and_missing_values(self):
        rows = normalize(self.item['payload'], 'stop', '11027', self.item['received_at_utc'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['estimated_delay_seconds'], 120)
        self.assertEqual(rows[0]['scheduled_lead_seconds'], 300)
        self.assertEqual(rows[0]['stop_key'], '11027')
        self.assertEqual(rows[0]['stop_number'], 'TEST-27')
        self.assertEqual(rows[0]['measurement_kind'], 'api_estimate')
        self.assertTrue(rows[1]['cancelled'])
        self.assertIsNone(rows[1]['bus_key'])
        self.assertIsNone(rows[1]['estimated_delay_seconds'])
        self.assertEqual(rows[1]['time_issues']['estimated'], 'missing')

    def test_dst_and_offsets(self):
        self.assertIsNone(api_time('2026-11-01T01:30:00')[0])
        self.assertIsNone(api_time('2026-03-08T02:30:00')[0])
        self.assertEqual(api_time('2026-09-14T08:00:00')[0].hour, 13)
        self.assertEqual(api_time('2026-09-14T08:00:00-05:00')[0].hour, 13)

    def test_append_replay_and_tracking(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'replay.sqlite'
            store = Store(path)
            try:
                result = replay(store, FIXTURE)
                self.assertEqual(result['errors'], 0)
                self.assertEqual(result['observations'], 5)
                # Both successive estimates of the same departure survive.
                delays = [r[0] for r in store.connection.execute("SELECT estimated_delay_seconds FROM observations WHERE scheduled_stop_key='fake-event-1' ORDER BY received_at_utc")]
                self.assertEqual(delays, [120, 180, None])
                trip = self.bundle['responses'][2]
                row = normalize(trip['payload'], 'trip', trip['resource_id'], trip['received_at_utc'])[0]
                self.assertEqual(row['time_issues']['scheduled'], 'date_missing')
                self.assertEqual(row['estimated_departure_raw'], '08:08:00')
                self.assertEqual(row['previous_trip_key'], 'fake-previous')
                self.assertFalse(status(path)['live_collection_verified'])
                now = datetime(2026, 9, 14, tzinfo=timezone.utc)
                store.track([{'trip_key':'a'}, {'trip_key':'b'}], now, 2)
                store.mark_trip('a', now)
                self.assertEqual(store.queued_trips(now, 1), ['b'])
                self.assertEqual(store.queued_trips(now + timedelta(minutes=3), 8), [])
            finally:
                store.close()

    def test_schema_error_keeps_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'test.sqlite')
            attempt = dict(http_status=200, error_kind=None, payload={'unexpected':'data'},
                           request_started_at_utc='2026-09-14T13:00:00+00:00', received_at_utc='2026-09-14T13:00:01+00:00',
                           endpoint='stops/11027/schedule.json', parameters={})
            try:
                rows, error = store.record('c', 'synthetic_fixture', 'stop', '11027', attempt)
                self.assertEqual((rows, error), ([], 'schema_error'))
                self.assertIn('unexpected', store.connection.execute('SELECT payload_json FROM snapshots').fetchone()[0])
            finally:
                store.close()

    def test_exclusive_database_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.sqlite'
            with collector_lock(path):
                with self.assertRaises(RuntimeError):
                    with collector_lock(path):
                        pass

    def test_retry_cooldown_and_secret_redaction(self):
        class Response(io.BytesIO):
            status = 200
            headers = {}
        clock = [0.0]
        calls = []
        def sleep(seconds):
            clock[0] += seconds
        def open_response(request, timeout):
            calls.append(clock[0])
            if len(calls) == 1:
                raise HTTPError(request.full_url, 429, 'rate limit', {'Retry-After':'10'}, io.BytesIO(b'{"api-key":"secret"}'))
            return Response(b'{"link":"https://example.test/?api-key=secret&x=1"}')
        client = ApiClient('secret', {'requests_per_minute':40, 'max_retries':2, 'timeout_seconds':1},
                           opener=open_response, monotonic=lambda:clock[0], sleep=sleep)
        attempts = list(client.attempts('stops/11027/schedule.json', {}))
        self.assertEqual(calls, [0, 10])
        self.assertEqual([x['http_status'] for x in attempts], [429, 200])
        self.assertNotIn('secret', json.dumps(attempts))
        self.assertNotIn('s%2Fe', json.dumps(sanitize({'url':'s%2Fe', 'authorization':'token'}, 's/e')))

    def test_authentication_failure_recordable_and_not_retried(self):
        def rejected(request, timeout):
            raise HTTPError(request.full_url, 401, 'rejected', {}, io.BytesIO(b'{}'))
        client = ApiClient('secret', {'requests_per_minute':40, 'max_retries':2, 'timeout_seconds':1}, opener=rejected)
        attempts = client.attempts('stops/11027/schedule.json', {})
        self.assertEqual(next(attempts)['http_status'], 401)
        with self.assertRaises(AuthenticationError):
            next(attempts)


if __name__ == '__main__':
    unittest.main()
