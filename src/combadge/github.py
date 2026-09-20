from __future__ import annotations

import base64
import json
import hashlib
import secrets
import threading
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import publishable_files
from .store import Store


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def commit_sha(payload: dict) -> str:
    """Compute the unsigned Git object ID before sending a create request."""
    lines = ['tree ' + payload['tree']]
    lines.extend('parent ' + p for p in payload['parents'])
    for role in ('author', 'committer'):
        person = payload[role]
        stamp = int(datetime.fromisoformat(person['date'].replace('Z', '+00:00')).timestamp())
        lines.append(f"{role} {person['name']} <{person['email']}> {stamp} +0000")
    raw = ('\n'.join(lines) + '\n\n' + payload['message']).encode()
    return hashlib.sha1(b'commit ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


@dataclass
class GitHubClient:
    token: str
    base_url: str = "https://api.github.com"
    opener: Any = urllib.request.urlopen

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(self.base_url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=30) as response:
                raw = response.read(); return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try: detail = exc.read().decode('utf-8', 'replace')[:500]
            finally: exc.close()
            raise GitHubError(f"GitHub API {exc.code}: {detail}", exc.code) from exc

    def validate_owner(self, expected: str | None) -> str:
        login = self.request("GET", "/user")["login"]
        if expected and login.casefold() != expected.casefold(): raise GitHubError("token does not belong to configured owner")
        return login


class Publisher:
    def __init__(self, client: GitHubClient, store: Store, owner: str):
        self.client, self.store, self.owner = client, store, owner
        self.lock = threading.RLock()

    def publish(self, key: str, source: Path, description: str) -> dict:
        with self.lock:
            return self._publish(key, source, description)

    def _publish(self, key: str, source: Path, description: str) -> dict:
        job = self.store.publish_by_key(key)
        if job["phase"] == "complete": return job
        repo, visibility = job["repository"], job["visibility"]
        base = f"/repos/{self.owner}/{repo}"
        try:
            # Validate all candidate content before any remote side effects.
            files = list(publishable_files(source))
            if not files: raise GitHubError('no publishable source files')
            if job["phase"] == "prepared":
                marker = 'combadge-publication:' + secrets.token_hex(24)
                self.store.update_publish(key, 'repository_creating', creation_marker=marker)
                job = self.store.publish_by_key(key)
            if job['phase'] == 'repository_creating':
                try: result = self.client.request('GET', base)
                except GitHubError as exc:
                    if exc.status != 404: raise
                    result = self.client.request('POST', '/user/repos', {
                        'name': repo, 'description': description[:200] + ' [' + job['creation_marker'] + ']',
                        'private': visibility == 'private', 'auto_init': True})
                if (job['creation_marker'] not in (result.get('description') or '')
                    or result.get('owner', {}).get('login', '').casefold() != self.owner.casefold()
                    or result.get('private') != (visibility == 'private')):
                    raise GitHubError('repository collision: publication ownership could not be verified')
                self.store.update_publish(key, "repository_created", github_url=result.get("html_url"))
                job = self.store.publish_by_key(key)
            repository = self.client.request("GET", base)
            branch = urllib.parse.quote(repository["default_branch"], safe="")
            ref = self.client.request("GET", f"{base}/git/ref/heads/{branch}")
            parent = ref["object"]["sha"]
            if job["commit_sha"]:
                if parent != job["commit_sha"]:
                    self.client.request("PATCH", f"{base}/git/refs/heads/{branch}", {"sha": job["commit_sha"], "force": False})
                self.store.update_publish(key, "complete", error=None)
                return self.store.publish_by_key(key)
            if job['commit_payload']:
                return self._commit_and_ref(key, base, branch, json.loads(job['commit_payload']))
            blobs = []
            for name, content in files:
                result = self.client.request("POST", f"/repos/{self.owner}/{repo}/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
                blobs.append({"path": name, "mode": "100644", "type": "blob", "sha": result["sha"]})
            self.store.update_publish(key, "blobs_uploaded")
            tree = self.client.request("POST", f"/repos/{self.owner}/{repo}/git/trees", {"tree": blobs})
            self.store.update_publish(key, "tree_created")
            identity = {'name': 'Combadge', 'email': f'{self.owner}@users.noreply.github.com',
                        'date': datetime.fromtimestamp(int(job['created_at']), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
            payload = {'message': 'Publish agent result\n', 'tree': tree['sha'], 'parents': [parent],
                       'author': identity, 'committer': identity}
            self.store.update_publish(key, 'commit_prepared', commit_payload=json.dumps(payload))
            return self._commit_and_ref(key, base, branch, payload)
        except Exception as exc:
            # Keep the last durable phase so restart recovery can resume it.
            self.store.update_publish(key, self.store.publish_by_key(key)["phase"], error=str(exc))
            raise

    def _commit_and_ref(self, key: str, base: str, branch: str, payload: dict) -> dict:
        expected = commit_sha(payload)
        try: commit = self.client.request('GET', f'{base}/git/commits/{expected}')
        except GitHubError as exc:
            if exc.status != 404: raise
            commit = self.client.request('POST', f'{base}/git/commits', payload)
        if commit.get('sha') != expected: raise GitHubError('provider commit differs from deterministic object ID')
        self.store.update_publish(key, 'commit_created', commit_sha=expected)
        self.client.request('PATCH', f'{base}/git/refs/heads/{branch}', {'sha': expected, 'force': False})
        self.store.update_publish(key, 'complete', error=None)
        return self.store.publish_by_key(key)
