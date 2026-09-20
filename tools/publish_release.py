"""Explicit operator-only organization release; not a voice/agent publication route.

Run on the Pi so its credential never leaves the host. Default is read-only.
Only an allowlist of control-layer source, tests, and docs is published.
"""
import argparse
import base64
import hashlib
import json
import shutil
import stat
import tempfile
from urllib.parse import quote
from pathlib import Path

from combadge.artifacts import publishable_files
from combadge.github import GitHubClient, GitHubError, Publisher
from combadge.store import Store


class OrganizationClient(GitHubClient):
    organization: str

    def request(self, method, path, body=None):
        if method == 'POST' and path == '/user/repos':
            path = '/orgs/' + self.organization + '/repos'
        return super().request(method, path, body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--organization', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--visibility', choices=['private', 'public'], default='private')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if stat.S_IMODE(args.token_file.stat().st_mode) & 0o077:
        raise PermissionError('token file must be private to its owner')
    client = OrganizationClient(args.token_file.read_text().strip())
    client.organization = args.organization
    org = client.request('GET', '/orgs/' + args.organization)
    if org['login'].casefold() != args.organization.casefold():
        raise RuntimeError('organization identity mismatch')
    base = f'/repos/{args.organization}/{args.repository}'
    if not args.publish:
        try:
            existing = client.request('GET', base)
            print(json.dumps({'organization': org['login'], 'repository_exists': True,
                              'private': existing['private']}))
        except GitHubError as exc:
            if exc.status != 404: raise
            print(json.dumps({'organization': org['login'], 'repository_exists': False}))
        return
    # Deliberately exclude nested application checkouts, .env files, state and artifacts.
    paths = [args.source / name for name in ('README.md', 'E2E-RESULTS.md', 'pyproject.toml')]
    for directory in ('src/combadge', 'tests', 'tools'):
        paths.extend(sorted((args.source / directory).glob('*.py')))
    if any(not p.is_file() or p.is_symlink() for p in paths):
        raise RuntimeError('release source is missing or contains links')
    with tempfile.TemporaryDirectory(prefix='combadge-release-') as temporary:
        staging = Path(temporary)
        for path in paths:
            dest = staging / path.relative_to(args.source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        expected = dict(publishable_files(staging))
        if len(expected) != len(paths): raise RuntimeError('release contains excluded files')
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        store = Store(args.state_dir / 'release.sqlite3')
        try:
            task_id = 'CDX-RELEASE'
            if not store.get_task(task_id): store.create_task(task_id, 'Publish reviewed control-layer release', 0)
            key = hashlib.sha256(f'{args.organization}/{args.repository}/{args.visibility}'.encode()).hexdigest()
            store.create_publish(task_id, args.repository, args.visibility, key)
            job = Publisher(client, store, args.organization).publish(key, staging, 'Pi-hosted Combadge coding-agent control layer')
            repository = client.request('GET', base)
            if repository['private'] != (args.visibility == 'private'): raise RuntimeError('visibility mismatch')
            branch = quote(repository['default_branch'], safe='')
            ref = client.request('GET', f'{base}/git/ref/heads/{branch}')
            if ref['object']['sha'] != job['commit_sha']: raise RuntimeError('published branch does not point to release commit')
            tree = client.request('GET', f"{base}/git/trees/{job['commit_sha']}?recursive=1")
            if tree.get('truncated'): raise RuntimeError('truncated verification tree')
            files = {entry['path']: entry for entry in tree['tree'] if entry['type'] == 'blob'}
            if set(files) != set(expected): raise RuntimeError('published paths differ from release')
            for name, entry in files.items():
                blob = client.request('GET', f"{base}/git/blobs/{entry['sha']}")
                if base64.b64decode(blob['content']) != expected[name]: raise RuntimeError('published content mismatch: ' + name)
            print(json.dumps({'url': job['github_url'], 'commit': job['commit_sha'],
                              'private': repository['private'], 'verified_files': len(files)}))
        finally:
            store.db.close()


if __name__ == '__main__':
    main()
