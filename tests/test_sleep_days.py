"""Regression: changing bedtime across midnight must not merge adjacent nights."""
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import app

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def event(start, end, title='Sleep', manual=False):
    a, b = map(datetime.fromisoformat, (start, end))
    return {'start': a.astimezone(timezone.utc), 'end': b.astimezone(timezone.utc),
            'hours': (b-a).total_seconds()/3600, 'title': title,
            'calendar': 'operations', 'focus': 'other', 'manual': manual}


class SleepDayTests(unittest.TestCase):
    def setUp(self):
        config = deepcopy(app.load_config())
        config['operations']['sleep_timezone'] = 'Asia/Tehran'
        patcher = patch.object(app, '_config', config)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch.object(app, 'utc_now', return_value=NOW)
    def test_adjacent_nights_and_nap_use_wake_date_without_mutating_events(self, _):
        events = [
            event('2026-10-10T04:00:00+03:30', '2026-10-10T12:00:00+03:30'),
            event('2026-10-10T23:30:00+03:30', '2026-10-11T07:30:00+03:30'),
            event('2026-10-11T14:00:00+03:30', '2026-10-11T15:00:00+03:30', 'Nap'),
        ]
        starts = [e['start'] for e in events]
        payload = app.build_operations_payload(events)
        expected = {'2026-10-10': 8, '2026-10-11': 9}
        self.assertEqual(payload['segments']['rest']['allocation_history'], expected)
        self.assertEqual(payload['allocation_history'], expected)
        frame = payload['timeframes']['daily']
        self.assertEqual({k: v for k, v in zip(frame['keys'], frame['rest']) if v}, expected)
        self.assertEqual(payload['total_hours'], 17)
        self.assertEqual(payload['rolling']['last_7_days']['total'], 17)
        self.assertEqual([e['start'] for e in events], starts)
        self.assertEqual(payload['sleep_sessions'][1]['start'], starts[1].isoformat())
        self.assertEqual(payload['sleep_sessions'][1]['sleep_date'], '2026-10-11')

    @patch.object(app, 'utc_now', return_value=NOW)
    def test_local_wake_date_week_boundary_and_non_sleep_entries(self, _):
        sleep = event('2026-10-11T18:30:00+03:30', '2026-10-12T02:30:00+03:30')
        # Wake occurs Sunday UTC, but Monday locally: include it in Monday's week.
        rest = event('2026-10-10T23:00:00+00:00', '2026-10-11T01:00:00+00:00', 'Rest')
        manual = event('2026-10-09T23:00:00+00:00', '2026-10-10T07:00:00+00:00', manual=True)
        payload = app.build_operations_payload([sleep, rest, manual])
        self.assertEqual(payload['allocation_history'], {'2026-10-09': 8, '2026-10-10': 2, '2026-10-12': 8})
        frame = payload['timeframes']['weekly']
        self.assertEqual(frame['rest'][frame['keys'].index('2026-W42')], 8)
        self.assertEqual(len(payload['sleep_sessions']), 1)

    def test_ongoing_uses_elapsed_end_and_local_today_is_in_chart(self):
        now = datetime.fromisoformat('2026-10-11T21:30:00+00:00')
        raw = {'summary': 'Sleep', 'start': {'dateTime': '2026-10-11T23:30:00+03:30'},
               'end': {'dateTime': '2026-10-12T08:00:00+03:30'}}
        parsed = app.parse_event(raw, now, 'operations')
        with patch.object(app, 'utc_now', return_value=now):
            payload = app.build_operations_payload([parsed])
        self.assertEqual(payload['sleep_sessions'][0]['sleep_date'], '2026-10-12')
        self.assertTrue(payload['sleep_sessions'][0]['in_progress'])
        self.assertEqual(payload['total_hours'], 1.5)
        self.assertEqual(payload['timeframes']['daily']['keys'][-1], '2026-10-12')
        self.assertEqual(payload['timeframes']['daily']['rest'][-1], 1.5)


if __name__ == '__main__':
    unittest.main()
