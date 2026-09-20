"""Opt-in real task and private GitHub publication; run on the Pi explicitly."""
import base64
import hashlib
import json
import time
from pathlib import Path

from combadge.client import ControlClient
from combadge.github import GitHubClient
from combadge.artifacts import publishable_files


def main():
    state = Path.home() / '.local/state/combadge-e2e'
    client = ControlClient(state / 'agents.sock')
    prepared = client.call('prepare_start', prompt=(
        'Create a tiny standalone Python project: hello.py prints COMBADGE_FULL_E2E_OK, '
        'test_hello.py uses unittest and subprocess to check that output, and README.md '
        'explains usage. Run the unittest in your hosted sandbox. Use no dependencies '
        'and no web search. Deliver source.zip and manifest.json as instructed.'))
    task = client.call('confirm_start', confirmation=prepared['confirmation'])
    print('STARTED', task['id'], flush=True)
    (state / 'full-e2e-task.json').write_text(json.dumps({'task_id': task['id']}))
    for _ in range(120):
        task = client.call('status', task_id=task['id'])
        print('STATUS', task['status'], flush=True)
        if task['status'] in {'completed', 'failed', 'cancelled', 'timed_out'}: break
        time.sleep(5)
    if task['status'] != 'completed':
        raise RuntimeError('Task did not complete: ' + str(task.get('terminal_summary')))
    repo = 'combadge-e2e-' + task['id'].lower()
    approval = client.call('prepare_publish', task_id=task['id'], repository=repo, visibility='private')
    print('PUBLISHING', repo, 'private', flush=True)
    result = client.call('confirm_publish', confirmation=approval['confirmation'])
    github = GitHubClient((Path.home() / '.local/state/combadge/github.pat').read_text().strip())
    owner = github.validate_owner('combadgehtn')
    base = f'/repos/{owner}/{repo}'
    metadata = github.request('GET', base)
    assert metadata['private'] is True
    ref = github.request('GET', base + '/git/ref/heads/' + metadata['default_branch'])
    assert ref['object']['sha'] == result['commit_sha']
    tree = github.request('GET', base + '/git/trees/' + result['commit_sha'] + '?recursive=1')
    actual = {item['path']: item for item in tree['tree'] if item['type'] == 'blob'}
    expected = dict(publishable_files(state / 'tasks' / task['id'] / 'source'))
    assert set(actual) == set(expected), (set(actual), set(expected))
    for name, data in expected.items():
        blob = github.request('GET', base + '/git/blobs/' + actual[name]['sha'])
        assert base64.b64decode(blob['content']) == data, name
    evidence = {'task_id': task['id'], 'url': result['github_url'],
                'commit_sha': result['commit_sha'], 'private': True,
                'verified_files': sorted(expected), 'manifest': task['manifest'],
                'usage': task['usage']}
    (state / 'full-e2e-result.json').write_text(json.dumps(evidence, indent=2))
    print('VERIFIED', json.dumps(evidence), flush=True)


if __name__ == '__main__': main()
