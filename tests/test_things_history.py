import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
import things


class ThingsHistoryTests(unittest.TestCase):
    def test_daily_history_is_not_truncated_by_recent_list(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.executescript('''CREATE TABLE TMTask (uuid TEXT,title TEXT,stopDate REAL,startDate REAL,deadline REAL,project TEXT,area TEXT,type INTEGER,trashed INTEGER,status INTEGER);
        CREATE TABLE TMArea (uuid TEXT,title TEXT);''')
        now = datetime.now(timezone.utc)
        yesterday = (now-timedelta(days=1)).replace(hour=12)
        for i in range(12):
            conn.execute('INSERT INTO TMTask VALUES (?,?,?,NULL,NULL,NULL,NULL,0,0,3)', (str(i),'done',things._core_data_cutoff(yesterday)))
        conn.execute('INSERT INTO TMTask VALUES (?,?,?,NULL,NULL,NULL,NULL,0,0,3)', ('future','future',things._core_data_cutoff(now+timedelta(days=1))))
        result=things._completed_stats(conn, {'completed_days':30,'recent_completed_limit':0,'areas':[],'tags':[]})
        self.assertEqual(result['recent'], [])
        self.assertEqual(result['daily'], {yesterday.date().isoformat():12})
        self.assertEqual(result['history_start'], (now-timedelta(days=365)).date().isoformat())
        conn.close()
