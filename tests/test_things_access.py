import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import things


class ThingsAccessTests(unittest.TestCase):
    def test_relative_override_does_not_depend_on_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tasks.sqlite'
            path.touch()
            with patch.object(things,'PROJECT_DIR',Path(directory)):
                self.assertEqual(things.detect_db_path('tasks.sqlite'),str(path))

    def test_permission_denial_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(things,'PROJECT_DIR',Path(directory)), patch.dict(os.environ,{},clear=True), patch.object(Path,'iterdir',side_effect=PermissionError('denied')):
                result=things.fetch_things_data({})
                self.assertFalse(result['available'])
                self.assertIn('Full Disk Access',result['error'])
                self.assertNotIn('not found',result['error'])

    def test_snapshot_includes_committed_wal_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tasks.sqlite'
            writer=sqlite3.connect(path)
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE tasks (title TEXT)')
                writer.execute("INSERT INTO tasks VALUES ('Recovered task')")
                writer.commit()
                with things.things_connection(path) as snapshot:
                    self.assertEqual(snapshot.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)
                    with self.assertRaises(sqlite3.OperationalError):
                        snapshot.execute('DELETE FROM tasks')
                self.assertEqual(writer.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)
            finally:
                writer.close()


class ThingsSnapshotCleanupTests(unittest.TestCase):
    def test_snapshot_and_sidecars_removed_on_success_and_query_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'source.sqlite'
                with sqlite3.connect(source) as conn:
                    conn.execute('CREATE TABLE tasks (id INTEGER)')
                temporary = None
                try:
                    with things.things_connection(source) as conn:
                        temporary = Path(conn.execute('PRAGMA database_list').fetchone()[2])
                        self.assertTrue(temporary.exists())
                        # Simulate sidecars left by SQLite's WAL handling.
                        Path(str(temporary)+'-wal').touch()
                        Path(str(temporary)+'-shm').touch()
                        if fail:
                            raise RuntimeError('query failed')
                except RuntimeError:
                    self.assertTrue(fail)
                self.assertFalse(temporary.parent.exists())
                self.assertTrue(source.exists())
