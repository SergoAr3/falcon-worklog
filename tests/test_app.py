from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import ApiError, TimeTrackerStore, build_server


class Clock:
    def __init__(self, timestamp=1793836800):
        self.timestamp = timestamp

    def __call__(self):
        return self.timestamp

    def advance(self, seconds):
        self.timestamp += seconds


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'time.sqlite3'
        self.clock = Clock()
        self.store = TimeTrackerStore(self.path, clock=self.clock)

    def dates(self):
        # The fixed clock is 2026-11-05 00:00 UTC.
        return ('2026-11-05', '2026-11-05')

    def test_employee_can_stop_and_start_another_session(self):
        employee = self.store.add_employee('Alex Morgan')
        self.store.start(employee['id'], 'Build dashboard')
        self.clock.advance(3600)
        first = self.store.stop(employee['id'])
        self.assertEqual(first['duration_seconds'], 3600)

        self.clock.advance(60)
        second = self.store.start(employee['id'], 'Review bugs')
        self.assertNotEqual(first['id'], second['id'])
        self.clock.advance(120)
        self.store.stop(employee['id'])

        result = self.store.report(*self.dates())
        self.assertEqual(result['total_seconds'], 3720)
        self.assertEqual(len(result['employees'][0]['records']), 2)
        self.assertIsNone(self.store.employees()[0]['active_entry'])

    def test_active_timer_survives_store_restart(self):
        employee = self.store.add_employee('Jordan Lee')
        self.store.start(employee['id'], 'Customer requests')
        self.clock.advance(180)

        restarted = TimeTrackerStore(self.path, clock=self.clock)
        active = restarted.employees()[0]['active_entry']
        self.assertEqual(active['description'], 'Customer requests')
        self.assertEqual(restarted.report(*self.dates())['total_seconds'], 180)

        self.clock.advance(40)
        stopped = restarted.stop(employee['id'])
        self.assertEqual(stopped['duration_seconds'], 220)
        self.assertEqual(TimeTrackerStore(self.path, clock=self.clock).report(*self.dates())['total_seconds'], 220)

    def test_empty_report_and_employee_without_records(self):
        empty = self.store.report(*self.dates())
        self.assertEqual(empty['total_seconds'], 0)
        self.assertEqual(empty['employees'], [])

        self.store.add_employee('Sam Rivera')
        after = self.store.report(*self.dates())
        self.assertEqual(after['total_seconds'], 0)
        self.assertEqual(len(after['employees']), 1)
        self.assertEqual(after['employees'][0]['records'], [])
        self.assertEqual(after['employees'][0]['total_seconds'], 0)

    def test_running_timer_appears_in_report_immediately(self):
        employee = self.store.add_employee('Taylor Reed')
        self.store.start(employee['id'], 'Check inventory')
        current = self.store.report(*self.dates())
        self.assertEqual(len(current['employees'][0]['records']), 1)
        self.assertTrue(current['employees'][0]['records'][0]['is_running'])
        self.assertEqual(current['employees'][0]['records'][0]['duration_seconds'], 0)

    def test_stopped_zero_second_session_is_visible(self):
        employee = self.store.add_employee('Taylor Reed')
        self.store.start(employee['id'], 'Quick check')
        self.store.stop(employee['id'])
        report = self.store.report(*self.dates())
        records = report['employees'][0]['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['duration_seconds'], 0)
        self.assertFalse(records[0]['is_running'])

    def test_report_clips_sessions_to_selected_days(self):
        # 2026-11-04 23:00 to 2026-11-05 01:00 UTC
        self.clock.timestamp -= 3600
        employee = self.store.add_employee('Alex Morgan')
        self.store.start(employee['id'], 'Overnight maintenance')
        self.clock.advance(7200)
        self.store.stop(employee['id'])

        previous = self.store.report('2026-11-04', '2026-11-04')
        current = self.store.report('2026-11-05', '2026-11-05')
        self.assertEqual(previous['total_seconds'], 3600)
        self.assertEqual(current['total_seconds'], 3600)
        self.assertEqual(self.store.report('2026-11-06', '2026-11-06')['total_seconds'], 0)

    def test_report_totals_per_employee(self):
        a = self.store.add_employee('Alex Morgan')
        b = self.store.add_employee('Sam Rivera')
        self.store.start(a['id'], 'Backend changes')
        self.clock.advance(30)
        self.store.start(b['id'], 'QA review')
        self.clock.advance(70)
        self.store.stop(a['id'])
        self.clock.advance(20)
        self.store.stop(b['id'])
        report = self.store.report(*self.dates())
        self.assertEqual(report['total_seconds'], 190)
        self.assertEqual([p['total_seconds'] for p in report['employees']], [100, 90])

    def test_timer_is_unique_per_employee(self):
        employee = self.store.add_employee('Jordan Lee')
        self.store.start(employee['id'], 'Draft spec')
        with self.assertRaises(ApiError) as raised:
            self.store.start(employee['id'], 'Second timer')
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(len(self.store.report(*self.dates())['employees'][0]['records']), 1)

    def test_concurrent_starts_create_only_one_active_timer(self):
        employee = self.store.add_employee('Jordan Lee')
        stores = [TimeTrackerStore(self.path, clock=self.clock) for _ in range(2)]
        barrier = threading.Barrier(2)

        def start_timer(store, description):
            barrier.wait(timeout=5)
            try:
                return 201, store.start(employee['id'], description)
            except ApiError as error:
                return error.status, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start_timer, store, f'Task {index}')
                       for index, store in enumerate(stores)]
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        started = next(entry for status, entry in results if status == 201)
        self.assertEqual(self.store.employees()[0]['active_entry']['id'], started['id'])
        records = self.store.report(*self.dates())['employees'][0]['records']
        self.assertEqual([record['id'] for record in records], [started['id']])

    def test_stopping_an_already_stopped_timer_preserves_the_record(self):
        employee = self.store.add_employee('Sam Rivera')
        self.store.start(employee['id'], 'Review dashboard')
        self.clock.advance(60)
        stopped = self.store.stop(employee['id'])
        self.clock.advance(120)

        with self.assertRaises(ApiError) as raised:
            self.store.stop(employee['id'])

        self.assertEqual(raised.exception.status, 409)
        records = self.store.report(*self.dates())['employees'][0]['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['id'], stopped['id'])
        self.assertEqual(records[0]['ended_at'], stopped['ended_at'])
        self.assertEqual(records[0]['duration_seconds'], 60)
        self.assertIsNone(self.store.employees()[0]['active_entry'])

    def test_report_uses_one_snapshot_during_concurrent_employee_creation(self):
        employee = self.store.add_employee('Alex Morgan')
        self.store.start(employee['id'], 'Original task')
        self.clock.advance(60)
        writer = TimeTrackerStore(self.path, clock=self.clock)
        original_connect = self.store._connect
        added_ids = []

        def write_between_report_queries(sql):
            if 'SELECT id, employee_id, description' in sql:
                added = writer.add_employee('Taylor Reed')
                writer.start(added['id'], 'New task')
                added_ids.append(added['id'])

        @contextmanager
        def report_connection():
            with original_connect() as db:
                # The second connection commits after employees have been read.
                db.set_trace_callback(write_between_report_queries)
                yield db

        with patch.object(self.store, '_connect', report_connection):
            report = self.store.report(*self.dates())

        self.assertEqual(len(added_ids), 1)
        self.assertEqual([person['id'] for person in report['employees']], [employee['id']])
        self.assertEqual(report['total_seconds'], 60)
        self.assertEqual(len(report['employees'][0]['records']), 1)
        refreshed = self.store.report(*self.dates())
        self.assertEqual(len(refreshed['employees']), 2)
        self.assertEqual(len(refreshed['employees'][1]['records']), 1)

    def test_input_validation(self):
        for name in ('', '  ', 'x' * 81, None):
            with self.subTest(name=name), self.assertRaises(ApiError):
                self.store.add_employee(name)
        employee = self.store.add_employee('Taylor Reed')
        for invalid in (' ', 'x' * 241, None):
            with self.subTest(description=invalid), self.assertRaises(ApiError):
                self.store.start(employee['id'], invalid)
        with self.assertRaises(ApiError):
            self.store.stop(9999)
        with self.assertRaises(ApiError):
            self.store.report('2026-11-06', '2026-11-05')
        with self.assertRaises(ApiError):
            self.store.report('not-a-date', '2026-11-05')
        with self.assertRaises(ApiError):
            self.store.report('2026-02-30', '2026-11-05')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'http.sqlite3'
        self.server = None
        self.start_server()

    def start_server(self):
        self.server = build_server('127.0.0.1', 0, self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def tearDown(self):
        self.stop_server()

    def request(self, path, data=None):
        headers = {}
        if data is not None:
            data = json.dumps(data).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(self.url + path, data=data, headers=headers)
        with urlopen(request, timeout=3) as response:
            body = response.read()
            if path in ('/', '/styles.css', '/app.js'):
                return response.status, body.decode('utf-8')
            return response.status, json.loads(body)

    def test_http_workflow_and_restart(self):
        status, page = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn('Worklog', page)
        self.assertEqual(self.request('/app.js')[0], 200)
        self.assertEqual(self.request('/styles.css')[0], 200)

        status, employee = self.request('/api/employees', {'name': 'Alex Morgan'})
        self.assertEqual(status, 201)
        status, started = self.request('/api/timer/start', {'employee_id': employee['id'], 'description': 'Review code'})
        self.assertEqual(status, 201)
        self.assertIsNone(started['ended_at'])

        self.stop_server()
        self.start_server()
        _, people = self.request('/api/employees')
        self.assertEqual(people['employees'][0]['active_entry']['description'], 'Review code')
        _, stopped = self.request('/api/timer/stop', {'employee_id': employee['id']})
        self.assertIsNotNone(stopped['ended_at'])
        self.assertEqual(len(self.request('/api/employees')[1]['employees']), 1)

    def test_http_invalid_input_returns_400(self):
        with self.assertRaises(HTTPError) as raised:
            self.request('/api/employees', {'name': ''})
        self.addCleanup(raised.exception.close)
        self.assertEqual(raised.exception.code, 400)
        _, report = self.request('/api/report?' + urlencode({'from': '2026-11-05', 'to': '2026-11-05'}))
        self.assertEqual(report['total_seconds'], 0)


if __name__ == '__main__':
    unittest.main()
