"""Transport integration tests; no live credentials or generated code execution."""
import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from combadge.app import Application
from combadge.client import ControlClient
from combadge.daemon import UnixHandler, UnixServer
from combadge.github import GitHubClient, GitHubError, Publisher, commit_sha
from combadge.service import AgentService
from combadge.store import Store
from combadge.web import WebHandler
from test_control import FakeAgents, config


class TransportE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config(Path(self.tmp.name))
        self.store = Store(self.cfg.database)
        self.provider = FakeAgents()
        self.app = Application(AgentService(self.cfg, self.store, self.provider))
        handler = type('TestWeb', (WebHandler,), {'app': self.app, 'ui_token': 'test-token'})
        unix_handler = type('TestUnix', (UnixHandler,), {'app': self.app})
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.unix = UnixServer(str(self.cfg.socket), unix_handler)
        for server in (self.http, self.unix):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.client = ControlClient(self.cfg.socket)
        self.csrf = hmac.new(b'test-token', b'csrf', hashlib.sha256).hexdigest()

    def tearDown(self):
        for server in (self.http, self.unix):
            server.shutdown(); server.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, authenticated=False, csrf=False):
        conn = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        headers = {}
        if authenticated: headers['Cookie'] = 'combadge_ui=test-token'
        if csrf: headers['X-CSRF-Token'] = self.csrf
        if payload is not None: headers['Content-Type'] = 'application/json'
        conn.request(method, path, None if payload is None else json.dumps(payload), headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_auth_cookie_and_csrf_rejection_before_side_effects(self):
        self.assertEqual(self.request('GET', '/api/tasks')[0], 401)
        status, headers, _ = self.request('GET', '/login?token=test-token')
        self.assertEqual(status, 303)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.request('POST', '/api/tasks/prepare', {'prompt': 'x'}, True)[0], 403)
        self.assertEqual(self.provider.created, 0)
        self.assertEqual(self.store.tasks(), [])

    def test_start_cross_transport_cancel_and_single_use_approval(self):
        status, _, raw = self.request('POST', '/api/tasks/prepare', {'prompt': 'fixture'}, True, True)
        self.assertEqual(status, 200)
        confirmation = json.loads(raw)['result']['confirmation']
        task = self.client.call('confirm_start', confirmation=confirmation)
        self.assertEqual(self.provider.created, 1)
        with self.assertRaises(RuntimeError):
            self.client.call('confirm_start', confirmation=confirmation)
        listed = json.loads(self.request('GET', '/api/tasks', authenticated=True)[2])
        self.assertEqual(listed[0]['id'], task['id'])
        approval = self.client.call('prepare_cancel', task_id=task['id'])
        status, _, raw = self.request('POST', '/api/tasks/cancel/confirm', approval, True, True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['result']['status'], 'cancelling')
        final = self.client.call('status', task_id=task['id'])
        self.assertEqual(final['status'], 'cancelled')
        self.assertEqual(self.provider.deleted, [task['session_id']])
        self.assertEqual(len(self.store.pending_notifications()), 1)

    def test_artifact_download_and_path_rejection(self):
        root = self.cfg.artifact_dir / 'CDX-TEST'
        root.mkdir()
        (root / 'manifest.json').write_text('{"fixture":true}')
        self.assertEqual(self.request('GET', '/artifacts/CDX-TEST/manifest.json')[0], 401)
        status, headers, body = self.request('GET', '/artifacts/CDX-TEST/manifest.json', authenticated=True)
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual(body, b'{"fixture":true}')
        self.assertEqual(self.request('GET', '/artifacts/../manifest.json', authenticated=True)[0], 404)


class PublicationContractE2E(unittest.TestCase):
    def test_bootstrap_publish_and_idempotent_retry(self):
        """Publish to an initialized branch through real HTTP."""
        calls = []
        repository = {}
        class GitHubFake(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, payload))
                if self.path == '/user/repos':
                    repository.update(payload)
                    repository.update(owner={'login': 'fixture'}, default_branch='main', html_url='https://github.com/fixture/example')
                    result, status = repository, 201
                elif self.path.endswith('/git/commits'):
                    result, status = {'sha': commit_sha(payload)}, 201
                elif self.path.endswith('/git/refs'):
                    result, status = {'message': 'Git Repository is empty.'}, 409
                else:
                    result, status = {'sha': 'fixture-sha'}, 201
                self.respond(status, result)
            def do_GET(self):
                if '/git/ref/' in self.path: self.respond(200, {'object': {'sha': 'bootstrap'}})
                elif '/git/commits/' in self.path or not repository: self.respond(404, {'message': 'not found'})
                else: self.respond(200, repository)
            def do_PATCH(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, payload))
                self.respond(200, {'object': {'sha': payload['sha']}})
            def respond(self, status, result):
                raw = json.dumps(result).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), GitHubFake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); store = Store(root / 'journal.db')
                try:
                    source = root / 'source'; source.mkdir(); (source / 'README.md').write_text('fixture')
                    store.create_task('CDX-TEST', 'fixture', 9999999999)
                    store.create_publish('CDX-TEST', 'example', 'private', 'test-key')
                    client = GitHubClient('fake-token', 'http://127.0.0.1:%d' % server.server_port)
                    publisher = Publisher(client, store, 'fixture')
                    publisher.publish('test-key', source, 'fixture')
                    self.assertTrue(calls[0][1]['auto_init'])
                    job = store.publish_by_key('test-key')
                    self.assertEqual(job['phase'], 'complete')
                    self.assertFalse(calls[-1][1]['force'])
                    count = len(calls)
                    publisher.publish('test-key', source, 'fixture')
                    self.assertEqual(len(calls), count)
                finally: store.db.close()
        finally:
            server.shutdown(); server.server_close()
