from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import time
import threading
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from .agents import APIError, paginate, terminal_result
from .artifacts import UnsafeArtifact, extract_source, validate_manifest
from .config import AgentConfig
from .github import Publisher
from .store import Store


class ServiceError(RuntimeError): pass
class ConfirmationError(ServiceError): pass
class CapacityError(ServiceError): pass

TERMINAL = {"completed", "failed", "cancelled", "timed_out", "expired"}


class AgentService:
    def __init__(self, config: AgentConfig, store: Store, agents: Any, publisher: Publisher | None = None, *, clock=time.time):
        self.config, self.store, self.agents, self.publisher, self.clock = config, store, agents, publisher, clock
        self.reconcile_lock = threading.RLock()

    @staticmethod
    def _task_id() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "CDX-" + "".join(secrets.choice(alphabet) for _ in range(4))

    def _confirmation(self, action: str, task_id: str | None, payload: dict) -> dict:
        token = secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.store.create_confirmation(digest, action, task_id, payload, self.clock() + 60)
        return {"confirmation": token, "expires_in": 60, **({"task_id": task_id} if task_id else {})}

    def _consume(self, token: str, action: str) -> dict:
        digest = hashlib.sha256(token.encode()).hexdigest()
        item = self.store.consume_confirmation(digest, action)
        if not item: raise ConfirmationError("confirmation is invalid, expired, or already used")
        return item

    def prepare_start(self, prompt: str) -> dict:
        if not prompt.strip(): raise ServiceError("prompt is required")
        if len(self.store.remote_work()) >= self.config.max_active: raise CapacityError("maximum active task count reached")
        task_id = self._task_id()
        return {**self._confirmation("start", task_id, {"prompt": prompt}), "message": f"Start coding task {task_id}?"}

    def confirm_start(self, token: str) -> dict:
        digest = hashlib.sha256(token.encode()).hexdigest()
        try: item = self.store.reserve_start(digest, self.config.max_active, self.config.timeout_minutes * 60)
        except OverflowError as exc: raise CapacityError(str(exc)) from exc
        if not item: raise ConfirmationError("confirmation is invalid, expired, or already used")
        task_id, prompt = item['task_id'], item['prompt']
        try:
            session = self.agents.create_session(task_id=task_id, installation_id=self.config.installation_id, prompt=prompt, model=self.config.model)
            self.store.attach_session(task_id, session["id"])
        except Exception as exc:
            # A lost response is not proof that the provider rejected creation.
            # Keep the reservation and discover by metadata; never blindly POST again.
            self.store.add_event(task_id, 'create_uncertain', str(exc))
            self.store.update_task(task_id, current_activity='Awaiting session creation reconciliation')
            if isinstance(exc, APIError) and exc.status in {400, 401, 403, 404, 422}:
                self._finish(task_id, 'failed', 'Provider rejected session creation.', {})
                self.store.clear_remote(task_id)
            raise
        return self.status(task_id, reconcile=False)

    def list_tasks(self) -> list[dict]:
        for task in self.store.active_tasks(): self.reconcile(task["id"])
        return self.store.tasks()

    def status(self, task_id: str, *, reconcile: bool = True) -> dict:
        if reconcile: self.reconcile(task_id)
        task = self.store.get_task(task_id)
        if not task: raise ServiceError("unknown task")
        task["events"] = self.store.events(task_id)
        task["artifacts"] = self.artifacts(task_id)
        return task

    def steer(self, task_id: str, message: str) -> dict:
        task = self.store.get_task(task_id)
        if not task or task["status"] not in {"running"}: raise ServiceError("task is not running")
        self.agents.steer(task["session_id"], message)
        self.store.add_event(task_id, "steer", message)
        return self.status(task_id, reconcile=False)

    def prepare_cancel(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        if not task or task["status"] not in {"creating", "running"}: raise ServiceError("task is not active")
        return {**self._confirmation("cancel", task_id, {}), "message": f"Cancel coding task {task_id}?"}

    def confirm_cancel(self, token: str) -> dict:
        item = self._consume(token, "cancel"); task_id = item["task_id"]
        task = self.store.get_task(task_id)
        if not task or task["status"] not in {"creating", "running"}: raise ServiceError("task is not active")
        self.store.update_task(task_id, status="cancelling", current_activity="Cancellation requested")
        self.store.queue_cleanup(task_id, task['session_id'], cancel=True)
        if task["session_id"]: self.agents.cancel(task["session_id"])
        self.store.add_event(task_id, "cancel", "Cancellation accepted by provider")
        return self.status(task_id, reconcile=False)

    def prepare_publish(self, task_id: str, repository: str, visibility: str) -> dict:
        task = self.store.get_task(task_id)
        if not task or task["status"] != "completed": raise ServiceError("only completed tasks can be published")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository) or repository.startswith("."): raise ServiceError("invalid repository name")
        if visibility not in {"public", "private"}: raise ServiceError("visibility must be public or private")
        return {**self._confirmation("publish", task_id, {"repository": repository, "visibility": visibility}), "message": f"Publish {task_id} as {repository}, {visibility}?"}

    def confirm_publish(self, token: str) -> dict:
        if not self.publisher: raise ServiceError("GitHub publication is not configured")
        item = self._consume(token, "publish"); payload, task_id = item["payload"], item["task_id"]
        task = self.store.get_task(task_id)
        key = hashlib.sha256(f"{task_id}\0{payload['repository']}\0{payload['visibility']}".encode()).hexdigest()
        self.store.create_publish(task_id, payload["repository"], payload["visibility"], key)
        result = self.publisher.publish(key, self.config.artifact_dir / task_id / "source", task.get("manifest", {}).get("description", ""))
        self.store.notify(task_id, "published", f"{task_id} published at {result['github_url']}.")
        return result

    def reconcile(self, task_id: str) -> None:
        with self.reconcile_lock:
            self._reconcile(task_id)

    def _reconcile(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task['status'] in TERMINAL:
            self._cleanup_remote(task_id)
            return
        if self.clock() >= task["deadline"]:
            self.store.queue_cleanup(task_id, task['session_id'], cancel=True)
            self._finish(task_id, "timed_out", "Task reached its time limit.", {})
            self._cleanup_remote(task_id)
            return
        if not task['session_id']: return
        if task['status'] == 'cancelling': self.agents.cancel(task['session_id'])
        session = self.agents.get_session(task["session_id"])
        history = list(paginate(lambda cursor: self.agents.list_history(task["session_id"], cursor)))
        result = terminal_result(session, history)
        activity = session.get("current_activity") or session.get("status")
        self.store.update_task(task_id, provider_status=session.get("status"), current_activity=activity)
        if result:
            status, summary, usage = result
            if status == "completed":
                try:
                    self._capture(task)
                    summary = self.store.get_task(task_id)['manifest']['final_summary']
                except Exception as exc:
                    status, summary = "failed", f"Unsafe or missing task output: {exc}"
            self.store.queue_cleanup(task_id, task['session_id'])
            self._finish(task_id, status, summary, usage)
            try: self._cleanup_remote(task_id)
            except Exception as exc: self.store.add_event(task_id, "cleanup_error", str(exc))

    def _cleanup_remote(self, task_id: str) -> None:
        work = next((w for w in self.store.remote_work() if w['task_id'] == task_id), None)
        if not work or not work['session_id']: return
        session_id = work['session_id']
        try:
            if work['cancel_required']:
                self.agents.cancel(session_id)
                session = self.agents.get_session(session_id)
                history = list(paginate(lambda cursor: self.agents.list_history(session_id, cursor)))
                if not terminal_result(session, history): return
            self.agents.delete_session(session_id)
        except APIError as exc:
            if exc.status != 404: raise
        self.store.clear_remote(task_id)

    def recover(self) -> None:
        pending = {w['task_id']: w for w in self.store.remote_work()}
        for session in paginate(lambda cursor: self.agents.list_sessions(installation_id=self.config.installation_id, cursor=cursor)):
            task_id = session.get("metadata", {}).get("combadge_task")
            if task_id in pending and not pending[task_id]['session_id']:
                task = self.store.get_task(task_id)
                if task and task['status'] in {'creating', 'cancelling'}: self.store.attach_session(task_id, session['id'])
                else:
                    self.store.queue_cleanup(task_id, session['id'], cancel=True)
                    if task: self.store.update_task(task_id, session_id=session['id'])
                pending[task_id]['session_id'] = session['id']
        for work in self.store.remote_work():
            try: self.reconcile(work['task_id'])
            except Exception as exc:
                if self.store.get_task(work['task_id']): self.store.add_event(work['task_id'], 'recovery_error', str(exc))
        if self.publisher:
            for job in self.store.incomplete_publishes():
                task = self.store.get_task(job["task_id"])
                if not task: continue
                try:
                    result = self.publisher.publish(job["idempotency_key"], self.config.artifact_dir / task["id"] / "source", (task.get("manifest") or {}).get("description", ""))
                    self.store.notify(task["id"], "published", f"{task['id']} published at {result['github_url']}.")
                except Exception as exc:
                    self.store.add_event(task["id"], "publish_recovery_error", str(exc))

    def _capture(self, task: dict) -> None:
        root = self.config.artifact_dir / task["id"]
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        items = list(paginate(lambda cursor: self.agents.list_artifacts(task['session_id'], cursor)))
        def artifact_name(item: dict) -> str:
            return item.get("name") or PurePosixPath(item.get("path", "")).name
        source = next((a for a in items if artifact_name(a) in {"source.zip", "source.tar.gz", "source.tgz"}), None)
        manifest = next((a for a in items if artifact_name(a) == "manifest.json"), None)
        if not source or not manifest: raise UnsafeArtifact("source archive and manifest.json are required")
        source_data = self.agents.download_artifact(task["session_id"], source["id"])
        manifest_data = self.agents.download_artifact(task["session_id"], manifest["id"], max_bytes=1024 * 1024)
        manifest_value = validate_manifest(manifest_data)
        extract_source(source_data, root / "source")
        (root / "source.archive").write_bytes(source_data); (root / "source.archive").chmod(0o600)
        (root / "manifest.json").write_bytes(manifest_data); (root / "manifest.json").chmod(0o600)
        self.store.update_task(task["id"], manifest_json=json.dumps(manifest_value))

    def _finish(self, task_id: str, status: str, summary: str, usage: dict) -> None:
        self.store.finish_task(task_id, status, summary, usage, self.clock() + self.config.retention_hours * 3600)

    def artifacts(self, task_id: str) -> list[dict]:
        root = self.config.artifact_dir / task_id
        if not root.exists(): return []
        return [{"name": p.name, "size": p.stat().st_size} for p in root.iterdir() if p.is_file() and p.name in {"source.archive", "manifest.json"}]

    def cleanup(self) -> None:
        for task_id in self.store.cleanup(self.clock()):
            root = self.config.artifact_dir / task_id
            if root.is_dir(): shutil.rmtree(root)
