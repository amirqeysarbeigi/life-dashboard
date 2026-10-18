"""Work calendar calculations and API failure/cache boundaries."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import app

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def event(hours, days=0, manual=False):
    return {'title': 'Client project', 'calendar': 'work', 'hours': hours,
            'start': NOW - timedelta(days=days), 'focus': 'other', 'manual': manual}


class WorkTests(unittest.TestCase):
    @patch.object(app, 'utc_now', return_value=NOW)
    def test_periods_tasks_and_manual_adjustments(self, _):
        payload = app.build_work_payload([event(2), event(3, 1), event(4, 40), event(10, manual=True)])
        self.assertEqual(payload['total_hours'], 19)
        self.assertEqual(payload['this_week'], {'hours': 15, 'sessions': 2})
        self.assertEqual(payload['rolling']['last_30_days']['total'], 15)
        self.assertEqual(payload['averages']['hours_per_session_30d'], 2.5)
        self.assertEqual(payload['tasks'][0]['hours'], 9)
        self.assertEqual(payload['tasks'][0]['sessions'], 3)
        self.assertEqual(sum(payload['allocation_history'].values()), 19)
        self.assertIn('work', payload['timeframes']['monthly'])

    def test_empty_calendar_is_available_zero(self):
        data = app.build_work_payload([])
        self.assertEqual(data['total_hours'], 0)
        self.assertEqual(data['tasks'], [])
        self.assertEqual(data['averages']['hours_per_session_30d'], 0)

    def test_elapsed_time_only(self):
        def raw(start, end):
            return {'summary': 'Work', 'start': {'dateTime': start.isoformat()}, 'end': {'dateTime': end.isoformat()}}
        ongoing = app.parse_event(raw(NOW-timedelta(hours=2), NOW+timedelta(hours=3)), NOW, 'work')
        self.assertEqual(ongoing['hours'], 2)
        self.assertIsNone(app.parse_event(raw(NOW+timedelta(hours=1), NOW+timedelta(hours=2)), NOW, 'work'))
        self.assertIsNone(app.parse_event({'start': {'date': '2026-09-30'}, 'end': {'date': '2026-10-01'}}, NOW, 'work'))

    @patch.object(app, 'utc_now', return_value=NOW)
    def test_calendar_timezone_is_normalized_for_utc_charts(self, _):
        parsed = app.parse_event({
            'summary': 'Late work',
            'start': {'dateTime': '2026-09-30T00:30:00+03:30'},
            'end': {'dateTime': '2026-09-30T01:30:00+03:30'},
        }, NOW, 'work')
        self.assertEqual(parsed['start'].isoformat(), '2026-09-29T21:00:00+00:00')
        payload = app.build_work_payload([parsed])
        self.assertEqual(payload['allocation_history'], {'2026-09-29': 1.0})
        self.assertEqual(payload['tasks'][0]['last_session'], '2026-09-29')
        self.assertEqual(payload['day_of_week']['total'][1], 1.0)

    def test_route_errors(self):
        for error, status in [(ValueError('Calendar not found: work'), 404), (RuntimeError('Offline'), 500)]:
            with patch.object(app, 'get_work_analytics', side_effect=error):
                response = app.app.test_client().get('/api/work')
                self.assertEqual(response.status_code, status)
                self.assertIn('error', response.json)

    @patch.object(app, 'get_calendar_service', return_value=object())
    @patch.object(app, 'fetch_named_calendar_events', return_value=[])
    def test_cache_force_refresh_and_failed_refresh_preserve_cache(self, fetch, _):
        with patch.object(app, '_work_cache', {'payload': None, 'fetched_at': None}):
            first = app.get_work_analytics()
            cached = app.get_work_analytics()
            self.assertTrue(cached['meta']['from_cache'])
            self.assertFalse(first['meta']['from_cache'])
            self.assertEqual(fetch.call_count, 1)
            app.get_work_analytics(force_refresh=True)
            self.assertEqual(fetch.call_count, 2)
            fetch.side_effect = RuntimeError('Offline')
            with self.assertRaises(RuntimeError):
                app.get_work_analytics(force_refresh=True)
            self.assertTrue(app.get_work_analytics()['meta']['from_cache'])


if __name__ == '__main__':
    unittest.main()
