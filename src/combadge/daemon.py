from __future__ import annotations

import json
import os
import socketserver
import threading
import time
from http.server import ThreadingHTTPServer

from .agents import AgentsClient
from .app import Application
from .config import AgentConfig
from .github import GitHubClient, Publisher
from .service import AgentService
from .store import Store
from .web import WebHandler


class UnixHandler(socketserver.StreamRequestHandler):
    app: Application
    def handle(self) -> None:
        raw = self.rfile.readline(1024 * 1024)
        try: result = self.app.request(json.loads(raw))
        except Exception as exc: result = {"ok": False, "error": str(exc)}
        self.wfile.write(json.dumps(result).encode() + b"\n")


class UnixServer(socketserver.ThreadingUnixStreamServer): daemon_threads = True


class Monitor:
    """SSE provides activity; independent polling remains authoritative."""
    def __init__(self, service: AgentService):
        self.service = service
        self.streams: dict[str, threading.Thread] = {}
        self.stopped = threading.Event()

    def consume(self, task: dict) -> None:
        try:
            for event, data in self.service.agents.stream(task['session_id']):
                current = self.service.store.get_task(task['id'])
                if self.stopped.is_set() or not current or current['status'] not in {'running', 'cancelling'}: break
                kind = data.get('type') or event
                excerpt = str(data.get('delta') or data.get('text') or kind)[:1000]
                self.service.store.add_event(task['id'], kind, excerpt)
        except Exception as exc:
            self.service.store.add_event(task['id'], 'stream_disconnected', str(exc))

    def tick(self) -> None:
        try: self.service.recover()
        except Exception as exc:
            # A listing outage must not prevent deadlines or known-session cleanup.
            for work in self.service.store.remote_work():
                try: self.service.reconcile(work['task_id'])
                except Exception as error: self.service.store.add_event(work['task_id'], 'reconcile_error', str(error))
        for task in self.service.store.active_tasks():
            if task['session_id'] and (task['id'] not in self.streams or not self.streams[task['id']].is_alive()):
                thread = threading.Thread(target=self.consume, args=(task,), daemon=True)
                self.streams[task['id']] = thread
                thread.start()
        self.streams = {key: value for key, value in self.streams.items() if value.is_alive()}
        self.service.cleanup()

    def run(self) -> None:
        while not self.stopped.is_set():
            try: self.tick()
            except Exception: pass  # Keep supervision alive across transient local failures.
            self.stopped.wait(10)


def build_service(config: AgentConfig) -> AgentService:
    config.initialize(); store = Store(config.database)
    agents = AgentsClient(config.read_secret(config.openai_key_file), config.openai_base_url)
    publisher = None
    if config.github_token_file.exists():
        github = GitHubClient(config.read_secret(config.github_token_file), config.github_base_url)
        owner = github.validate_owner(config.github_owner); publisher = Publisher(github, store, owner)
    return AgentService(config, store, agents, publisher)


def serve(config: AgentConfig | None = None) -> None:
    config = config or AgentConfig.from_env(); service = build_service(config); app = Application(service)
    try: service.recover()
    except Exception as exc:
        service.store.add_event(service.store.active_tasks()[0]["id"], "recovery_error", str(exc)) if service.store.active_tasks() else None
    WebHandler.app = app; WebHandler.ui_token = config.ui_token_file.read_text().strip()
    http = ThreadingHTTPServer(("127.0.0.1", config.ui_port), WebHandler)
    if config.socket.exists(): config.socket.unlink()
    try:
        UnixHandler.app = app; unix = UnixServer(str(config.socket), UnixHandler); config.socket.chmod(0o600)
    except BaseException:
        http.server_close()
        raise
    config.pid_file.write_text(str(os.getpid())); config.pid_file.chmod(0o600)
    threading.Thread(target=unix.serve_forever, daemon=True).start()
    monitor = Monitor(service)
    threading.Thread(target=monitor.run, daemon=True).start()
    try: http.serve_forever()
    finally:
        monitor.stopped.set()
        http.server_close(); unix.shutdown(); unix.server_close()
        config.socket.unlink(missing_ok=True); config.pid_file.unlink(missing_ok=True)
