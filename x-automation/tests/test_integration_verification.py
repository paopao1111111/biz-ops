import hashlib
import hmac
import http.cookiejar
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path('/private/tmp/x-browse-v2-staging/controller/app.py')


class RealHTTPProtocol2Verification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='x-http-verification-', dir='/private/tmp')
        cls.db = str(Path(cls.tmp) / 'console.db')
        cls.admin_password = 'fixture-admin-only'
        cls.session_secret = 'fixture-session-only'
        cls.worker_secret = 'fixture-worker-only'
        os.environ.update(
            X_CONSOLE_DB=cls.db,
            X_CONSOLE_HOST='127.0.0.1',
            X_CONSOLE_PORT='0',
            X_CONSOLE_ADMIN_PASSWORD=cls.admin_password,
            X_CONSOLE_SESSION_SECRET=cls.session_secret,
            X_CONSOLE_WORKER_SECRET=cls.worker_secret,
            X_CONSOLE_MAX_CONCURRENCY='3',
        )
        spec = importlib.util.spec_from_file_location('controller_http_verification', APP)
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.app.init()
        cls.server = cls.app.ThreadingHTTPServer(('127.0.0.1', 0), cls.app.H)
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        jar = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        body = urllib.parse.urlencode({'password': cls.admin_password}).encode()
        req = urllib.request.Request(cls.base + '/login', data=body, headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        cls.opener.open(req).read()
        index = cls.opener.open(cls.base + '/').read().decode()
        marker = '<meta name="csrf-token" content="'
        cls.csrf = index.split(marker, 1)[1].split('"', 1)[0]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def admin(self, method, path, data=None):
        body = None if method == 'GET' else json.dumps(data or {}, separators=(',', ':')).encode()
        headers = {'Accept': 'application/json'}
        if method != 'GET':
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf})
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        with self.opener.open(req) as response:
            return json.loads(response.read())

    def worker(self, method, path, data=None, token=None, worker_id='fixture-worker'):
        body = b'' if method == 'GET' else json.dumps(data or {}, separators=(',', ':')).encode()
        timestamp = str(int(time.time()))
        canonical = '\n'.join((timestamp, method, path, hashlib.sha256(body).hexdigest()))
        headers = {
            'Accept': 'application/json',
            'X-Worker-ID': worker_id,
            'X-Timestamp': timestamp,
            'X-Signature': hmac.new(self.worker_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest(),
        }
        if method != 'GET': headers['Content-Type'] = 'application/json'
        if token: headers['X-Execution-Token'] = token
        req = urllib.request.Request(self.base + path, data=None if method == 'GET' else body, headers=headers, method=method)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())

    def test_protocol2_lifecycle_pause_retry_and_overview_consistency(self):
        heartbeat = self.worker('POST', '/api/worker/heartbeat', {
            'status': 'idle', 'capacity': 3, 'available_slots': 3, 'protocol_version': '2.0',
            'capabilities': ['execution_tokens', 'cleanup_confirmation', 'heartbeat_reconciliation'],
            'executions': [], 'active_job_ids': [], 'active_profile_ids': [], 'active_proxy_ports': [],
        })
        self.assertEqual(heartbeat['directives'], [])
        started = self.admin('POST', '/api/x/accounts/10/start', {'duration_minutes': 5})['data']
        claim = self.worker('POST', '/api/worker/claim', {})['job']
        self.assertEqual(claim['id'], started['job_id'])
        token = claim['execution_token']
        self.assertTrue(token)
        self.worker('POST', f"/api/worker/jobs/{claim['id']}/start", {'execution_token': token}, token)
        progress = self.worker('POST', f"/api/worker/jobs/{claim['id']}/progress", {
            'execution_token': token, 'phase': 'search', 'current_source': 'search:fixture',
            'elapsed_seconds': 23, 'search_count': 1, 'trending_count': 0, 'unique_items': 1,
            'events': [{'event_type': 'fixture_progress', 'detail': '<safe>'}],
            'items': [{'item_key': '1', 'source': 'search:fixture', 'author_handle': '@fixture',
                       'text': '<script>not executed</script>', 'url': 'https://x.com/fixture/status/1'}],
        }, token)
        self.assertTrue(progress['accepted'])
        complete = self.worker('POST', f"/api/worker/jobs/{claim['id']}/complete", {
            'execution_token': token, 'status': 'failed', 'actual_seconds': 23, 'error': 'network error',
            'failure_code': 'network', 'failure_detail': 'fixture network detail', 'cleanup_confirmed': False,
        }, token)
        self.assertEqual(complete['directive'], 'stop_and_cleanup')
        terminal_progress = self.worker('POST', f"/api/worker/jobs/{claim['id']}/progress", {
            'execution_token': token, 'elapsed_seconds': 99, 'events': [{'event_type': 'late'}],
        }, token)
        self.assertFalse(terminal_progress['accepted'])
        self.assertEqual(terminal_progress['directive'], 'stop_and_cleanup')
        cleanup = self.worker('POST', f"/api/worker/jobs/{claim['id']}/complete", {
            'execution_token': token, 'status': 'failed', 'cleanup_confirmed': True,
        }, token)
        self.assertEqual(cleanup['directive'], 'forget')
        ghost = self.worker('POST', '/api/worker/heartbeat', {
            'status': 'busy', 'capacity': 3, 'available_slots': 2, 'protocol_version': '2.0',
            'capabilities': ['execution_tokens', 'cleanup_confirmation', 'heartbeat_reconciliation'],
            'executions': [{'job_id': claim['id'], 'execution_token': token, 'cleanup_confirmed': True,
                            'profile_id': claim['profile_id'], 'proxy_port': claim['proxy_port']}],
        })
        self.assertEqual(ghost['directives'][0]['directive'], 'forget')
        overview = self.admin('GET', '/api/x/overview')['data']
        self.assertEqual(overview['summary']['capacity'], overview['workload']['capacity'])
        self.assertEqual(overview['summary']['available_slots'], overview['workload']['available_slots'])
        run = self.admin('GET', f"/api/x/runs/{started['run_id']}")['data']
        self.assertEqual(len(run['events']), 1)
        self.assertEqual(len(run['items']), 1)
        self.assertEqual(run['run']['actual_seconds'], 23)
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE runs SET retry_eligible=1,retry_not_before=?,retry_block_reason='global_pause',cleanup_confirmed=1 WHERE id=?", (int(time.time()) - 1, started['run_id']))
            c.execute("UPDATE accounts SET auto_schedule_enabled=1,daily_window_start='00:00',daily_window_end='23:59' WHERE id=10")
            c.commit()
        self.worker('POST', '/api/worker/heartbeat', {
            'status': 'idle', 'capacity': 3, 'available_slots': 3, 'protocol_version': '2.0',
            'capabilities': ['execution_tokens', 'cleanup_confirmation', 'heartbeat_reconciliation'],
            'executions': [], 'active_job_ids': [], 'active_profile_ids': [], 'active_proxy_ports': [],
        })
        before = len(self.admin('GET', '/api/x/overview')['data']['recent_runs'])
        time.sleep(16)
        self.assertEqual(len(self.admin('GET', '/api/x/overview')['data']['recent_runs']), before)
        self.admin('POST', '/api/x/schedule/resume', {})
        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            c.execute('BEGIN IMMEDIATE')
            self.app.scheduler_fill(c, int(time.time()))
            c.commit()
        self.worker('POST', '/api/worker/heartbeat', {
            'status': 'idle', 'capacity': 3, 'available_slots': 3, 'protocol_version': '2.0',
            'capabilities': ['execution_tokens', 'cleanup_confirmation', 'heartbeat_reconciliation'],
            'executions': [], 'active_job_ids': [], 'active_profile_ids': [], 'active_proxy_ports': [],
        })
        time.sleep(16)
        with sqlite3.connect(self.db) as c:
            retries = c.execute('SELECT COUNT(*) FROM runs WHERE retry_of_run_id=?', (started['run_id'],)).fetchone()[0]
        self.assertEqual(retries, 1)
        self.admin('POST', '/api/x/schedule/pause', {})
        duplicate = self.worker('POST', f"/api/worker/jobs/{claim['id']}/complete", {
            'execution_token': token, 'status': 'failed', 'actual_seconds': 299, 'cleanup_confirmed': True,
        }, token)
        self.assertTrue(duplicate['already_completed'])
        with sqlite3.connect(self.db) as c:
            used = c.execute('SELECT used_seconds FROM daily_plans WHERE account_id=10 AND plan_date=?', (self.app.today(),)).fetchone()[0]
        self.assertEqual(used, 23)


if __name__ == '__main__':
    unittest.main()
