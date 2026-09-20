from __future__ import annotations

import hashlib
import hmac
import html
import json
import mimetypes
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .app import Application


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'><title>{html.escape(title)}</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#101418;color:#e8edf2}}a{{color:#7dcfff}}article{{border:1px solid #3b4652;border-radius:8px;padding:1rem;margin:1rem 0}}input,textarea,select,button{{font:inherit;padding:.5rem;margin:.25rem;background:#202832;color:#fff;border:1px solid #607080}}textarea{{width:95%}}code,pre{{white-space:pre-wrap}}.muted{{color:#a8b3bd}}</style></head><body>{body}</body></html>""".encode()


class WebHandler(BaseHTTPRequestHandler):
    app: Application
    ui_token: str

    @property
    def csrf(self) -> str: return hmac.new(self.ui_token.encode(), b"csrf", hashlib.sha256).hexdigest()

    def _authenticated(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie")); value = cookie.get("combadge_ui")
        return bool(value and hmac.compare_digest(value.value, self.ui_token))

    def _send(self, status: int, body: bytes, content_type="application/json", headers: dict | None = None) -> None:
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'")
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(body)

    def _json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path); query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/login" and "token" in query and hmac.compare_digest(query["token"][0], self.ui_token):
            self._send(303, b"", headers={"Set-Cookie": f"combadge_ui={self.ui_token}; HttpOnly; SameSite=Strict; Path=/", "Location": "/"}); return
        if not self._authenticated(): self._send(401, _page("Sign in", "<h1>Combadge Agents</h1><p>Use the Pi-local access-token URL.</p>"), "text/html"); return
        if parsed.path == "/api/tasks": self._send(200, json.dumps(self.app.call("list", {})).encode()); return
        if parsed.path.startswith("/api/tasks/"):
            task_id = parsed.path.split("/")[3]; self._send(200, json.dumps(self.app.call("status", {"task_id": task_id})).encode()); return
        if parsed.path.startswith("/artifacts/"):
            parts = parsed.path.split("/"); task_id, name = parts[2], parts[3] if len(parts) > 3 else ""
            if name not in {"source.archive", "manifest.json"}: self._send(404, b"not found", "text/plain"); return
            path = self.app.service.config.artifact_dir / task_id / name
            try: path.resolve().relative_to(self.app.service.config.artifact_dir.resolve())
            except ValueError: self._send(404, b"not found", "text/plain"); return
            if not path.is_file(): self._send(404, b"not found", "text/plain"); return
            self._send(200, path.read_bytes(), mimetypes.guess_type(name)[0] or "application/octet-stream", {"Content-Disposition": f'attachment; filename="{name}"'}); return
        if parsed.path == "/": self._index(); return
        if parsed.path.startswith("/tasks/"): self._detail(parsed.path.split("/")[2]); return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if not self._authenticated(): self._send(401, b'{"error":"unauthorized"}'); return
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/ui/"):
            length = int(self.headers.get("Content-Length", "0")); form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
            if not hmac.compare_digest(form.pop("csrf", ""), self.csrf): self._send(403, b"invalid csrf", "text/plain"); return
            try:
                if path == "/ui/start": result, confirm_action = self.app.call("prepare_start", form), "/ui/start/confirm"
                elif path == "/ui/start/confirm": self.app.call("confirm_start", form); return self._redirect("/")
                elif path == "/ui/steer": self.app.call("steer", form); return self._redirect(f"/tasks/{form['task_id']}")
                elif path == "/ui/cancel": result, confirm_action = self.app.call("prepare_cancel", form), "/ui/cancel/confirm"
                elif path == "/ui/cancel/confirm": self.app.call("confirm_cancel", form); return self._redirect(f"/tasks/{form['task_id']}")
                elif path == "/ui/publish": result, confirm_action = self.app.call("prepare_publish", form), "/ui/publish/confirm"
                elif path == "/ui/publish/confirm": self.app.call("confirm_publish", form); return self._redirect(f"/tasks/{form['task_id']}")
                else: self._send(404, b"not found", "text/plain"); return
                task = html.escape(result.get("task_id", form.get("task_id", "")))
                body = f"<h1>Confirm action</h1><p>{html.escape(result['message'])}</p><form method=post action='{confirm_action}'><input type=hidden name=csrf value='{self.csrf}'><input type=hidden name=confirmation value='{html.escape(result['confirmation'])}'><input type=hidden name=task_id value='{task}'><button type=submit>Confirm</button></form>"
                self._send(200, _page("Confirm", body), "text/html"); return
            except Exception as exc:
                self._send(400, _page("Error", f"<h1>Action failed</h1><p>{html.escape(str(exc))}</p>"), "text/html"); return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.csrf): self._send(403, b'{"error":"invalid csrf"}'); return
        routes = {"/api/tasks/prepare": "prepare_start", "/api/tasks/confirm": "confirm_start", "/api/tasks/steer": "steer", "/api/tasks/cancel/prepare": "prepare_cancel", "/api/tasks/cancel/confirm": "confirm_cancel", "/api/tasks/publish/prepare": "prepare_publish", "/api/tasks/publish/confirm": "confirm_publish"}
        action = routes.get(urllib.parse.urlparse(self.path).path)
        if not action: self._send(404, b'{"error":"not found"}'); return
        result = self.app.request({"action": action, "data": self._json()})
        self._send(200 if result["ok"] else 400, json.dumps(result).encode())

    def _redirect(self, location: str) -> None: self._send(303, b"", headers={"Location": location})

    def _index(self) -> None:
        cards = []
        for task in self.app.call("list", {}):
            cards.append(f"<article><h2><a href='/tasks/{html.escape(task['id'])}'>{html.escape(task['id'])}</a> — {html.escape(task['status'])}</h2><p>{html.escape(task['prompt'][:240])}</p><p class=muted>{html.escape(task.get('current_activity') or '')}</p></article>")
        start = f"<article><h2>Start task</h2><form method=post action='/ui/start'><input type=hidden name=csrf value='{self.csrf}'><textarea name=prompt required></textarea><button type=submit>Prepare task</button></form></article>"
        self._send(200, _page("Combadge Agents", "<h1>Combadge Agents</h1>" + start + "".join(cards)), "text/html")

    def _detail(self, task_id: str) -> None:
        try: task = self.app.call("status", {"task_id": task_id})
        except Exception: self._send(404, b"not found", "text/plain"); return
        events = "\n".join(f"{e['kind']}: {e['excerpt']}" for e in task["events"])
        files = " ".join(f"<a href='/artifacts/{html.escape(task_id)}/{html.escape(a['name'])}'>{html.escape(a['name'])}</a>" for a in task["artifacts"])
        controls = ""
        if task["status"] == "running":
            controls += f"<form method=post action='/ui/steer'><input type=hidden name=csrf value='{self.csrf}'><input type=hidden name=task_id value='{html.escape(task_id)}'><textarea name=message required></textarea><button>Steer</button></form><form method=post action='/ui/cancel'><input type=hidden name=csrf value='{self.csrf}'><input type=hidden name=task_id value='{html.escape(task_id)}'><button>Prepare cancellation</button></form>"
        if task["status"] == "completed":
            controls += f"<form method=post action='/ui/publish'><input type=hidden name=csrf value='{self.csrf}'><input type=hidden name=task_id value='{html.escape(task_id)}'><input name=repository required placeholder='repository-name'><select name=visibility><option>private</option><option>public</option></select><button>Prepare publication</button></form>"
        body = f"<p><a href='/'>← Tasks</a></p><h1>{html.escape(task_id)} — {html.escape(task['status'])}</h1><article><h2>Prompt</h2><pre>{html.escape(task['prompt'])}</pre><p>Deadline: {task['deadline']}</p><p>Activity: {html.escape(task.get('current_activity') or '')}</p></article><article><h2>Controls</h2>{controls}</article><article><h2>Result</h2><p>{html.escape(task.get('terminal_summary') or '')}</p><pre>{html.escape(json.dumps(task.get('usage'), indent=2))}</pre><pre>{html.escape(json.dumps((task.get('manifest') or {}).get('test_results'), indent=2))}</pre>{files}</article><article><h2>Events</h2><pre>{html.escape(events)}</pre></article>"
        self._send(200, _page(task_id, body), "text/html")

    def log_message(self, format: str, *args: Any) -> None: pass
