"""Acceptance tests for failure paths. Failures represent unresolved defects."""
import io
import json
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from combadge.artifacts import publishable_files
from combadge.artifacts import extract_source, UnsafeArtifact
from combadge.agents import AgentsClient, APIError, iter_sse
from combadge.daemon import Monitor
from combadge.github import GitHubError, Publisher, commit_sha
from combadge.live import deliver_agent_notifications
from combadge.service import AgentService, CapacityError, ConfirmationError
from combadge.store import Store
from test_control import StoreAndServiceTests


class FailurePaths(StoreAndServiceTests):
    # Inherit the fixture, but the runner below can select only new tests.
    def reopen(self):
        self.store.db.close()
        self.store = Store(self.cfg.database, clock=lambda: self.now[0])
        self.service = AgentService(self.cfg, self.store, self.agents, clock=lambda: self.now[0])

    def outputs(self, entries=None, manifest=None):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            for name, value in (entries or {'main.py': 'fixture'}).items(): z.writestr(name, value)
        self.agents.artifact_data = {
            'source.zip': archive.getvalue(),
            'manifest.json': json.dumps(manifest if manifest is not None else {
                'repository_name': 'fixture', 'description': 'fixture', 'test_results': 'passed',
                'entry_points': ['main.py'], 'final_summary': 'Fixture completed successfully.'}).encode()}

    def finish_provider(self, task):
        self.agents.turns[task['session_id']] = [{'status': 'completed', 'final_output': 'done'}]

    def test_restart_running_task_does_not_create_again(self):
        task = self.start(); self.reopen(); self.service.recover()
        self.assertEqual(self.agents.created, 1)
        self.assertEqual(self.store.get_task(task['id'])['session_id'], task['session_id'])

    def test_crash_after_cloud_create_recovers_without_duplicate(self):
        approval = self.service.prepare_start('fixture')
        with patch.object(self.store, 'attach_session', side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit): self.service.confirm_start(approval['confirmation'])
        self.reopen(); self.service.recover()
        self.assertEqual(self.agents.created, 1)
        self.assertEqual(len(self.store.active_tasks()), 1)
        self.assertIsNotNone(self.store.active_tasks()[0]['session_id'])

    def test_lost_create_response_recovers_remote_session(self):
        original = self.agents.create_session
        def lost(**kwargs):
            original(**kwargs)
            raise TimeoutError('response lost after provider committed')
        with patch.object(self.agents, 'create_session', side_effect=lost):
            with self.assertRaises(TimeoutError): self.start()
        self.reopen(); self.service.recover()
        task = self.store.tasks()[0]
        self.assertIsNotNone(task['session_id'], 'Orphan cloud session not recovered')

    def test_three_tasks_independent_cancel_and_fourth_rejection(self):
        tasks = [self.start(str(i)) for i in range(3)]
        with self.assertRaises(CapacityError): self.start('fourth')
        for task in tasks: self.service.steer(task['id'], 'guidance')
        approval = self.service.prepare_cancel(tasks[1]['id'])
        self.service.confirm_cancel(approval['confirmation']); self.service.reconcile(tasks[1]['id'])
        self.assertEqual([self.store.get_task(t['id'])['status'] for t in tasks], ['running','cancelled','running'])

    def test_simultaneous_confirmations_enforce_capacity(self):
        approvals = [self.service.prepare_start(str(i)) for i in range(4)]
        barrier = threading.Barrier(4)
        original = self.store.active_tasks
        def confirm(approval):
            barrier.wait(timeout=5)
            try: return self.service.confirm_start(approval['confirmation'])
            except CapacityError: return None
            except Exception as exc: return exc
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(confirm, approvals))
        self.assertLessEqual(len(original()), 3, 'Capacity check and insertion are not atomic')
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.assertEqual(self.agents.created, 3)

    def test_ambiguous_creation_is_not_reposted(self):
        with patch.object(self.agents, 'create_session', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError): self.start()
        self.reopen()
        for _ in range(3): self.service.recover()
        self.assertEqual(self.agents.created, 0)
        self.assertEqual(len(self.store.remote_work()), 1)

    def test_delayed_cancel_holds_capacity_until_terminal(self):
        task = self.start(); self.now[0] = task['deadline']
        with patch.object(self.agents, 'cancel', return_value={}):
            self.service.reconcile(task['id'])
            self.assertEqual(self.store.get_task(task['id'])['status'], 'timed_out')
            self.assertEqual(self.agents.deleted, [])
            self.assertEqual(len(self.store.remote_work()), 1)
        self.service.reconcile(task['id'])
        self.assertEqual(self.store.remote_work(), [])

    def test_cleanup_survives_retention_and_restart(self):
        self.outputs(); task = self.start(); self.finish_provider(task)
        with patch.object(self.agents, 'delete_session', side_effect=TimeoutError):
            self.service.reconcile(task['id'])
        self.now[0] = self.store.get_task(task['id'])['retention_deadline']
        self.service.cleanup(); self.reopen(); self.service.recover()
        self.assertIsNone(self.store.get_task(task['id']))
        self.assertIn(task['session_id'], self.agents.deleted)
        self.assertEqual(self.store.remote_work(), [])

    def test_cancel_failure_keeps_durable_intent(self):
        task = self.start(); approval = self.service.prepare_cancel(task['id'])
        with patch.object(self.agents, 'cancel', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError): self.service.confirm_cancel(approval['confirmation'])
        self.reopen(); self.service.recover()
        self.assertEqual(self.store.get_task(task['id'])['status'], 'cancelled')

    def test_already_deleted_session_clears_cleanup(self):
        task = self.start(); self.now[0] = task['deadline']
        with patch.object(self.agents, 'cancel', side_effect=APIError('gone', 404)):
            self.service.reconcile(task['id'])
        self.assertEqual(self.store.remote_work(), [])

    def test_manifest_summary_replaces_generic_completion(self):
        self.outputs(); task = self.start(); self.finish_provider(task)
        self.service.reconcile(task['id'])
        self.assertEqual(self.store.get_task(task['id'])['terminal_summary'], 'Fixture completed successfully.')

    def test_download_rejects_over_limit_and_closes_stream(self):
        stream = io.BytesIO(b'12345'); client = AgentsClient('fake')
        with patch.object(client, '_request', return_value=stream):
            with self.assertRaises(APIError): client.download_artifact('s', 'a', max_bytes=4)
        self.assertTrue(stream.closed)

    def test_environment_network_is_explicitly_disabled(self):
        client = AgentsClient('control-secret')
        with patch.object(client, '_request', return_value={'id': 's'}) as request:
            client.create_session(task_id='t', installation_id='i', prompt='p', model=self.cfg.model)
        body = request.call_args.args[2]
        self.assertEqual(body['environment']['network'], {'access': 'disabled'})
        self.assertNotIn('control-secret', json.dumps(body))

    def test_sse_records_activity_without_deciding_completion(self):
        task = self.start(); monitor = Monitor(self.service)
        with patch.object(self.agents, 'stream', create=True, return_value=iter([
            ('message', {'type': 'agent.output', 'delta': 'Running tests'}),
            ('message', {'type': 'agent.session.idle'})])):
            monitor.consume(task)
        self.assertIn('Running tests', [e['excerpt'] for e in self.store.events(task['id'])])
        self.assertEqual(self.store.get_task(task['id'])['status'], 'running')
        self.assertEqual(list(iter_sse(io.BytesIO(b'data: [DONE]'))), [])

    def test_listing_outage_does_not_block_deadlines(self):
        task = self.start(); self.now[0] = task['deadline']
        with patch.object(self.agents, 'list_sessions', side_effect=TimeoutError): Monitor(self.service).tick()
        self.assertEqual(self.store.get_task(task['id'])['status'], 'timed_out')

    def test_timeout_retries_failed_cancel_after_restart(self):
        task = self.start(); self.now[0] = task['deadline']
        with patch.object(self.agents, 'cancel', side_effect=TimeoutError('network outage')):
            with self.assertRaises(TimeoutError): self.service.reconcile(task['id'])
        self.reopen(); self.service.recover(); self.service.reconcile(task['id'])
        self.assertIn(task['session_id'], self.agents.cancelled, 'Timed-out cloud work remains running')

    def test_failed_delete_retried_after_restart(self):
        self.outputs(); task = self.start(); self.finish_provider(task)
        with patch.object(self.agents, 'delete_session', side_effect=TimeoutError('network outage')):
            self.service.reconcile(task['id'])
        self.reopen(); self.service.recover(); self.service.reconcile(task['id'])
        self.assertIn(task['session_id'], self.agents.deleted, 'Terminal cleanup is never retried')

    def test_expired_start_cancel_publish_approvals(self):
        start = self.service.prepare_start('expired')
        self.now[0] += 61
        with self.assertRaises(ConfirmationError): self.service.confirm_start(start['confirmation'])
        task = self.start(); cancel = self.service.prepare_cancel(task['id'])
        self.now[0] += 61
        with self.assertRaises(ConfirmationError): self.service.confirm_cancel(cancel['confirmation'])
        self.outputs(); self.finish_provider(task); self.service.reconcile(task['id'])
        publish = self.service.prepare_publish(task['id'], 'fixture', 'public')
        self.assertIn('public', publish['message']); self.assertIn('fixture', publish['message'])
        self.now[0] += 61
        self.service.publisher = object()
        with self.assertRaises(ConfirmationError): self.service.confirm_publish(publish['confirmation'])

    def test_malicious_archive_rejected_via_capture(self):
        self.outputs({'../escape': 'bad'}); task = self.start(); self.finish_provider(task)
        self.service.reconcile(task['id'])
        self.assertEqual(self.store.get_task(task['id'])['status'], 'failed')
        self.assertFalse((self.root / 'escape').exists())

    def test_malformed_manifest_rejected_via_capture(self):
        self.outputs(manifest={'not': 'a manifest'}); task = self.start(); self.finish_provider(task)
        self.service.reconcile(task['id'])
        self.assertEqual(self.store.get_task(task['id'])['status'], 'failed')

    def test_credentials_excluded_after_download(self):
        self.outputs({'main.py': 'fixture', '.env': 'FAKE_SECRET', '.env.production': 'FAKE_SECRET', 'id_rsa': 'FAKE_PRIVATE_KEY'})
        task = self.start(); self.finish_provider(task); self.service.reconcile(task['id'])
        names = dict(publishable_files(self.cfg.artifact_dir / task['id'] / 'source'))
        self.assertEqual(set(names), {'main.py'}, 'Credential variants would be published')

    def test_archive_size_and_file_count_limits(self):
        self.outputs({'a': 'a'*4096, 'b': 'b'})
        data = self.agents.artifact_data['source.zip']
        with self.assertRaises(UnsafeArtifact): extract_source(data, self.root/'size-limit', max_bytes=100)
        with self.assertRaises(UnsafeArtifact): extract_source(data, self.root/'count-limit', max_files=1)

    def test_download_uses_bounded_reads(self):
        class Stream(io.BytesIO):
            unbounded = False
            def read(self, size=-1):
                if size < 0: self.unbounded = True
                return super().read(size)
        stream = Stream(b'fixture')
        client = AgentsClient('fake')
        with patch.object(client, '_request', return_value=stream):
            client.download_artifact('session', 'artifact')
        self.assertFalse(stream.unbounded, 'Untrusted artifact is read into RAM without a bound')

    def test_notification_partial_failure_does_not_repeat_success(self):
        self.store.notify('one', 'terminal', 'one'); self.store.notify('two', 'terminal', 'two')
        spoken = []
        def speak(message):
            if message == 'two': raise ConnectionError('Live disconnected')
            spoken.append(message)
        with self.assertRaises(ConnectionError): deliver_agent_notifications(self.store, speak)
        self.reopen(); deliver_agent_notifications(self.store, spoken.append)
        self.assertEqual(spoken, ['one', 'two'], 'Successful announcement repeated after disconnect')

    def test_retention_boundary_preserves_then_removes(self):
        self.outputs(); task = self.start(); self.finish_provider(task); self.service.reconcile(task['id'])
        expiry = self.store.get_task(task['id'])['retention_deadline']
        self.now[0] = expiry - 1; self.service.cleanup()
        self.assertTrue((self.cfg.artifact_dir / task['id']).exists())
        self.reopen(); self.now[0] = expiry; self.service.cleanup()
        self.assertIsNone(self.store.get_task(task['id']))
        self.assertFalse((self.cfg.artifact_dir / task['id']).exists())
        self.assertEqual(self.store.pending_notifications(), [])


class FakeGitHub:
    def __init__(self):
        self.repo = False; self.creates = 0; self.commits = 0; self.ref = 'bootstrap'; self.fail = None
        self.info = {}; self.objects = {}
    def request(self, method, path, body=None):
        if method == 'POST' and path == '/user/repos':
            if self.repo: raise GitHubError('422 already exists')
            self.repo = True; self.creates += 1
            self.info = {**body, 'owner': {'login': 'fixture'}, 'default_branch': 'main', 'html_url': 'https://github.com/fixture/result'}
            result = self.info
        elif method == 'GET' and '/git/commits/' in path:
            if path.rsplit('/', 1)[-1] not in self.objects: raise GitHubError('not found', 404)
            result = self.objects[path.rsplit('/', 1)[-1]]
        elif method == 'GET' and '/git/ref/' in path: result = {'object': {'sha': self.ref}}
        elif method == 'GET':
            if not self.repo: raise GitHubError('not found', 404)
            result = self.info
        elif path.endswith('/git/commits'):
            self.commits += 1; result = {'sha': commit_sha(body)}
            self.objects[result['sha']] = result
        elif method == 'PATCH': self.ref = body['sha']; result = {}
        else: result = {'sha': 'blob-or-tree'}
        if self.fail and self.fail in path and method in {'POST','PATCH'}:
            self.fail = None
            raise SystemExit('crash after remote write, before journal update')
        return result


class PublicationFailures(StoreAndServiceTests):
    def test_commit_object_matches_git_hash_object(self):
        identity = {'name': 'Combadge', 'email': 'fixture@users.noreply.github.com', 'date': '1970-01-01T00:16:40Z'}
        payload = {'tree': 'a'*40, 'parents': ['b'*40], 'author': identity,
                   'committer': identity, 'message': 'Publish agent result\n'}
        # Independently checked with git hash-object -t commit --stdin.
        self.assertEqual(commit_sha(payload), 'e543a44b516238c6b6f95f5720c3f70a5746d352')

    def test_collision_never_changes_existing_repo(self):
        gh = FakeGitHub(); gh.repo = True
        self.store.create_task('CDX-PUB', 'fixture', 99999)
        self.store.create_publish('CDX-PUB', 'result', 'private', 'key')
        with self.assertRaises(GitHubError): Publisher(gh, self.store, 'fixture').publish('key', self.root, 'fixture')
        self.assertEqual(gh.commits, 0); self.assertEqual(gh.ref, 'bootstrap')

    def test_crash_replay_each_publication_phase(self):
        for phase in ('/user/repos', '/git/blobs', '/git/trees', '/git/commits', '/git/refs/'):
            with self.subTest(phase=phase):
                gh = FakeGitHub(); gh.fail = phase
                suffix = phase.replace('/', '-')
                task_id = 'CDX' + suffix
                self.store.create_task(task_id, 'fixture', 99999)
                self.store.create_publish(task_id, 'result', 'private', suffix)
                source = self.root / suffix; source.mkdir(); (source/'README.md').write_text('fixture')
                publisher = Publisher(gh, self.store, 'fixture')
                with self.assertRaises(SystemExit): publisher.publish(suffix, source, 'fixture')
                self.store.db.close()
                self.store = Store(self.cfg.database, clock=lambda: self.now[0])
                publisher = Publisher(gh, self.store, 'fixture')
                try: publisher.publish(suffix, source, 'fixture')
                except GitHubError as exc: self.fail('Crash recovery blocked: ' + str(exc))
                self.assertEqual(self.store.publish_by_key(suffix)['phase'], 'complete')
                self.assertEqual(gh.creates, 1)
                self.assertEqual(gh.commits, 1, 'Crash replay creates a duplicate commit')


def load_tests(loader, tests, pattern):
    # Avoid executing imported/inherited baseline tests twice.
    suite = unittest.TestSuite()
    for cls in (FailurePaths, PublicationFailures):
        for name in cls.__dict__:
            if name.startswith('test_'): suite.addTest(cls(name))
    return suite
