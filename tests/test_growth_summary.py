import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import app

class GrowthSummaryTests(unittest.TestCase):
    @patch.object(app, 'utc_now', return_value=datetime(2026,10,18,12,tzinfo=timezone.utc))
    def test_all_growth_in_summary_but_only_workouts_in_workout_results(self, now):
        events=[{'title':title,'hours':hours,'start':now.return_value,'calendar':'growth','focus':'other'} for title,hours in [('Gym',1),('Meditation',.5),('Journaling',.75)]]
        result=app.build_growth_payload(events)
        self.assertEqual(result['total_hours'],1)
        self.assertEqual(result['this_week']['sessions'],1)
        summary=result['summary']
        self.assertEqual(summary['total_hours'],2.25)
        self.assertEqual(summary['this_week']['hours'],2.25)
        for days in (1,7,30,90,365):
            self.assertEqual(summary['rolling'][f'last_{days}_days']['total'],2.25)
        self.assertEqual(summary['allocation_history'],{'2026-10-18':2.25})
        for period in ('daily','weekly'):
            self.assertEqual(sum(summary['timeframes'][period]['total']),2.25)
        mental=app.build_growth_payload(events[1:])
        self.assertEqual(mental['total_hours'],0)
        self.assertEqual(mental['summary']['total_hours'],1.25)
        self.assertEqual(app.build_growth_payload([])['summary']['total_hours'],0)
