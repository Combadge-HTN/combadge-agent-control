from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, session_id TEXT UNIQUE, prompt TEXT NOT NULL,
  status TEXT NOT NULL, provider_status TEXT, current_activity TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, deadline REAL NOT NULL,
  terminal_summary TEXT, usage_json TEXT, manifest_json TEXT,
  retention_deadline REAL, announced_at REAL
);
CREATE TABLE IF NOT EXISTS confirmations (
  digest TEXT PRIMARY KEY, action TEXT NOT NULL, task_id TEXT,
  payload_json TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at REAL
);
CREATE TABLE IF NOT EXISTS publish_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  repository TEXT NOT NULL, visibility TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
  phase TEXT NOT NULL, github_url TEXT, commit_sha TEXT, error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  kind TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL,
  delivered_at REAL, UNIQUE(task_id, kind)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  created_at REAL NOT NULL, kind TEXT NOT NULL, excerpt TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_notifications_delivery ON notifications(delivered_at);
CREATE TABLE IF NOT EXISTS remote_work (
  task_id TEXT PRIMARY KEY, session_id TEXT, cancel_required INTEGER NOT NULL DEFAULT 0
);
"""


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class Store:
    def __init__(self, path: Path, *, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.clock = clock
        upgrading = self.db.execute('PRAGMA user_version').fetchone()[0] < 1
        self.db.executescript(SCHEMA)
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(publish_jobs)')}
        for name in ('creation_marker', 'commit_payload'):
            if name not in columns:
                self.db.execute(f'ALTER TABLE publish_jobs ADD COLUMN {name} TEXT')
        # Backfill unfinished work when upgrading an existing installation.
        self.db.execute("INSERT OR IGNORE INTO remote_work(task_id,session_id) SELECT id,session_id FROM tasks WHERE status IN ('creating','running','cancelling')")
        if upgrading:
            self.db.execute("INSERT OR IGNORE INTO remote_work(task_id,session_id,cancel_required) SELECT id,session_id,CASE WHEN status IN ('timed_out','cancelled') THEN 1 ELSE 0 END FROM tasks WHERE session_id IS NOT NULL")
            self.db.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def create_task(self, task_id: str, prompt: str, deadline: float) -> None:
        now = self.clock()
        with self.transaction() as db:
            db.execute("INSERT INTO tasks(id,prompt,status,created_at,updated_at,deadline) VALUES(?,?, 'creating',?,?,?)", (task_id, prompt, now, now, deadline))
            db.execute('INSERT INTO remote_work(task_id) VALUES(?)', (task_id,))

    def reserve_start(self, digest: str, max_active: int, timeout: float) -> dict | None:
        """Consume approval and reserve capacity in one SQLite write transaction."""
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM confirmations WHERE digest=? AND action='start' AND consumed_at IS NULL AND expires_at>?", (digest, now)).fetchone()
            if not row: return None
            count = db.execute('SELECT count(*) FROM remote_work').fetchone()[0]
            if count >= max_active: raise OverflowError('maximum active task count reached')
            prompt = json.loads(row['payload_json'])['prompt']
            db.execute("INSERT INTO tasks(id,prompt,status,created_at,updated_at,deadline) VALUES(?,?,'creating',?,?,?)", (row['task_id'], prompt, now, now, now + timeout))
            db.execute('INSERT INTO remote_work(task_id) VALUES(?)', (row['task_id'],))
            db.execute('UPDATE confirmations SET consumed_at=? WHERE digest=?', (now, digest))
        return {'task_id': row['task_id'], 'prompt': prompt}

    def attach_session(self, task_id: str, session_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE tasks SET session_id=?,status=CASE WHEN status='creating' THEN 'running' ELSE status END,provider_status='active',updated_at=? WHERE id=? AND session_id IS NULL", (session_id, self.clock(), task_id))
            db.execute('UPDATE remote_work SET session_id=? WHERE task_id=?', (session_id, task_id))

    def queue_cleanup(self, task_id: str, session_id: str | None, cancel: bool = False) -> None:
        with self.transaction() as db:
            db.execute('INSERT INTO remote_work VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET session_id=excluded.session_id,cancel_required=excluded.cancel_required', (task_id, session_id, int(cancel)))

    @locked
    def remote_work(self) -> list[dict]:
        return [dict(r) for r in self.db.execute('SELECT * FROM remote_work')]

    def clear_remote(self, task_id: str) -> None:
        with self.transaction() as db:
            db.execute('DELETE FROM remote_work WHERE task_id=?', (task_id,))

    def update_task(self, task_id: str, **values: Any) -> None:
        allowed = {"status", "provider_status", "current_activity", "terminal_summary", "usage_json", "manifest_json", "retention_deadline", "announced_at", "session_id"}
        if not values or set(values) - allowed:
            raise ValueError("invalid task update")
        values["updated_at"] = self.clock()
        fields = ",".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            db.execute(f"UPDATE tasks SET {fields} WHERE id=?", (*values.values(), task_id))

    @locked
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(row) if row else None

    @locked
    def tasks(self, include_expired: bool = False) -> list[dict[str, Any]]:
        clause = "" if include_expired else "WHERE retention_deadline IS NULL OR retention_deadline>?"
        args = () if include_expired else (self.clock(),)
        return [self._task(row) for row in self.db.execute(f"SELECT * FROM tasks {clause} ORDER BY created_at DESC", args)]

    @locked
    def active_tasks(self) -> list[dict[str, Any]]:
        return [self._task(row) for row in self.db.execute("SELECT * FROM tasks WHERE status IN ('creating','running','cancelling') ORDER BY created_at")]

    def add_event(self, task_id: str, kind: str, excerpt: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO events(task_id,created_at,kind,excerpt) SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE id=?)", (task_id, self.clock(), kind, excerpt[:1000], task_id))
            db.execute('DELETE FROM events WHERE task_id=? AND id NOT IN (SELECT id FROM events WHERE task_id=? ORDER BY id DESC LIMIT 200)', (task_id, task_id))

    def finish_task(self, task_id: str, status: str, summary: str, usage: dict, retention: float) -> None:
        with self.transaction() as db:
            row = db.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['status'] in {'completed','failed','cancelled','timed_out','expired'}: return
            db.execute('UPDATE tasks SET status=?,terminal_summary=?,usage_json=?,retention_deadline=?,updated_at=? WHERE id=?',
                       (status, summary, json.dumps(usage), retention, self.clock(), task_id))
            db.execute('INSERT OR IGNORE INTO notifications(task_id,kind,message,created_at) VALUES(?,?,?,?)',
                       (task_id, 'terminal', f'{task_id} {status}: {summary[:180]}', self.clock()))

    @locked
    def events(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT created_at,kind,excerpt FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))
        return [dict(row) for row in reversed(rows.fetchall())]

    def create_confirmation(self, digest: str, action: str, task_id: str | None, payload: dict[str, Any], expires_at: float) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO confirmations VALUES(?,?,?,?,?,NULL)", (digest, action, task_id, json.dumps(payload), expires_at))

    def consume_confirmation(self, digest: str, action: str) -> dict[str, Any] | None:
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM confirmations WHERE digest=? AND action=? AND consumed_at IS NULL AND expires_at>?", (digest, action, now)).fetchone()
            if not row:
                return None
            db.execute("UPDATE confirmations SET consumed_at=? WHERE digest=?", (now, digest))
        result = dict(row); result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def notify(self, task_id: str, kind: str, message: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO notifications(task_id,kind,message,created_at) VALUES(?,?,?,?)", (task_id, kind, message, self.clock()))

    @locked
    def pending_notifications(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM notifications WHERE delivered_at IS NULL ORDER BY id")]

    def mark_notifications_delivered(self, ids: list[int]) -> None:
        if ids:
            with self.transaction() as db:
                db.executemany("UPDATE notifications SET delivered_at=? WHERE id=? AND delivered_at IS NULL", [(self.clock(), i) for i in ids])

    def create_publish(self, task_id: str, repository: str, visibility: str, key: str) -> dict[str, Any]:
        now = self.clock()
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO publish_jobs(task_id,repository,visibility,idempotency_key,phase,created_at,updated_at) VALUES(?,?,?,?, 'prepared',?,?)", (task_id, repository, visibility, key, now, now))
        return self.publish_by_key(key)

    @locked
    def publish_by_key(self, key: str) -> dict[str, Any]:
        return dict(self.db.execute("SELECT * FROM publish_jobs WHERE idempotency_key=?", (key,)).fetchone())

    @locked
    def incomplete_publishes(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM publish_jobs WHERE phase!='complete' ORDER BY id")]

    def update_publish(self, key: str, phase: str, **values: Any) -> None:
        allowed = {"github_url", "commit_sha", "error", "creation_marker", "commit_payload"}
        if set(values) - allowed: raise ValueError("invalid publish update")
        values = {"phase": phase, **values, "updated_at": self.clock()}
        with self.transaction() as db:
            db.execute("UPDATE publish_jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE idempotency_key=?", (*values.values(), key))

    def cleanup(self, now: float | None = None) -> list[str]:
        now = now or self.clock()
        with self.transaction() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE retention_deadline IS NOT NULL AND retention_deadline<=?", (now,))]
            for task_id in ids:
                db.execute("DELETE FROM events WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM notifications WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM publish_jobs WHERE task_id=?", (task_id,))
                db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return ids

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("usage_json", "manifest_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field)) if result[field] else None
        return result
