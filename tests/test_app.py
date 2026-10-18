"""Run with: venv/bin/python -m unittest discover -s tests"""
import os
import tempfile
import unittest
from pathlib import Path
import app


class AppBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_insights_script_has_explicit_public_route(self):
        with self.client.get('/insights.js') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'RelationshipStats', response.data)

    def test_landing_and_dashboard_routes(self):
        with self.client.get('/') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'My spaces', response.data)
            self.assertIn(b'href="/dashboard"', response.data)
        with self.client.get('/dashboard') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="tab-nav"', response.data)

    def test_shared_theme_is_served(self):
        with self.client.get('/theme.css') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn('text/css', response.content_type)
            self.assertIn(b'--accent:', response.data)

    def test_private_files_have_no_public_route(self):
        for path in ('/static/credentials.json', '/static/token.json', '/static/cache.db', '/credentials.json'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_home_and_config_are_independent_of_working_directory(self):
        previous = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                os.chdir(directory)
                with self.client.get('/') as response:
                    self.assertEqual(response.status_code, 200)
                self.assertTrue(Path(app.CONFIG_PATH).is_file())
                self.assertEqual(app.load_config()['operations']['calendar'], 'operations')
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
