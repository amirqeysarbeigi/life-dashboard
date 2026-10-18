"""Transport classification and conservation of Operations hours."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import app

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class OperationsTransportTests(unittest.TestCase):
    def test_whole_word_case_and_existing_rest_priority(self):
        cases = {
            'Go to campus': 'transport', 'GO home': 'transport',
            'go': 'transport', 'Plan: go!': 'transport',
            'yoga': 'daily_life', 'going home': 'daily_life',
            'Google meeting': 'daily_life', 'cargo': 'daily_life',
            'laundry': 'daily_life', 'go to sleep': 'rest',
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(app.classify_operations_event({'title': title}), expected)

    @patch.object(app, 'utc_now', return_value=NOW)
    def test_transport_is_separate_without_losing_or_duplicating_hours(self, _):
        events = [
            {'title': title, 'hours': hours, 'start': NOW, 'calendar': 'operations', 'focus': 'other'}
            for title, hours in [('Sleep', 8), ('Go to campus', 1), ('Go home', .5), ('Laundry', 2)]
        ]
        payload = app.build_operations_payload(events)
        self.assertEqual(payload['total_hours'], 11.5)
        self.assertEqual(payload['totals']['transport']['hours'], 1.5)
        self.assertEqual(payload['totals']['daily_life']['hours'], 2)
        self.assertEqual(payload['this_week']['categories']['transport']['hours'], 1.5)
        self.assertEqual(payload['last_30_days']['transport']['hours'], 1.5)
        self.assertEqual(payload['averages']['transport']['hours_per_week_30d'], .35)
        self.assertEqual(sum(payload['timeframes']['daily']['transport']), 1.5)
        self.assertEqual(sum(payload['timeframes']['weekly']['transport']), 1.5)
        self.assertEqual(payload['segments']['transport']['allocation_history'], {'2026-10-11': 1.5})
        self.assertEqual(sum(segment['total_hours'] for segment in payload['segments'].values()), 11.5)

    def test_empty_transport_remains_available(self):
        payload = app.build_operations_payload([])
        self.assertEqual(payload['totals']['transport']['hours'], 0)
        self.assertEqual(payload['averages']['transport']['hours_per_week_30d'], 0)
        self.assertEqual(payload['segments']['transport']['allocation_history'], {})


if __name__ == '__main__':
    unittest.main()


class FoodTests(unittest.TestCase):
    def test_meals_whole_words_and_priority(self):
        for title in ['Breakfast', 'LUNCH with friends', 'Dinner', 'Eating', 'Eat', 'Food', 'Meal', 'Snacks', 'Brunch', 'Supper']:
            with self.subTest(title=title):
                self.assertEqual(app.classify_operations_event({'title': title}), 'food')
        for title in ['Lunchbox shopping', 'Dinnerware', 'Theater']:
            self.assertEqual(app.classify_operations_event({'title': title}), 'daily_life')
        self.assertEqual(app.classify_operations_event({'title': 'Go to lunch'}), 'transport')

    @patch.object(app, 'utc_now', return_value=NOW)
    def test_meals_count_once_in_cards_charts_and_history(self, _):
        events = [{'title': title, 'hours': hours, 'start': NOW, 'calendar': 'operations'}
                  for title, hours in [('Breakfast', .5), ('Lunch', 1), ('Dinner', .5), ('Laundry', 1)]]
        data = app.build_operations_payload(events)
        self.assertEqual(data['total_hours'], 3)
        self.assertEqual(data['totals']['food']['hours'], 2)
        self.assertEqual(data['totals']['daily_life']['hours'], 1)
        self.assertEqual(data['this_week']['categories']['food']['hours'], 2)
        self.assertEqual(sum(data['timeframes']['daily']['food']), 2)
        self.assertEqual(sum(data['segments']['food']['allocation_history'].values()), 2)
        self.assertEqual(sum(s['total_hours'] for s in data['segments'].values()), 3)
