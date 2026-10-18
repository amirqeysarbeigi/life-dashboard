"""Synthetic data for frontend checks; never reads calendar or Things accounts."""
import json
import sys
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

now = app.utc_now()

def event(title, calendar, hours=2):
    return {'title': title, 'calendar': calendar, 'hours': hours,
            'start': now - timedelta(days=1), 'focus': 'other'}

education = [event('study', 'courses')]
payloads = {
    'analytics': app.build_analytics_payload({'courses': education, 'research': []}, education),
    'growth': app.build_growth_payload([event('workout', 'growth'), event('Meditation', 'growth')]),
    'operations': app.build_operations_payload([event('sleep', 'operations', 8), event('laundry', 'operations'), event('Go to campus', 'operations', 1), event('Lunch', 'operations', .5)]),
    'work': app.build_work_payload([event('<b>Client work</b>', 'work', 3)]),
    'fun': app.build_fun_payload([event('fun', 'fun')]),
    'fucked': app.build_fucked_payload([]),
    'things': {'available': False, 'reason': 'Not installed'},
}
for data in payloads.values():
    data['meta'] = {'fetched_at': now.isoformat(), 'from_cache': False}
print(json.dumps(payloads))
