from __future__ import annotations

import io
import json
import tarfile
import tempfile
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from combadge.agents import iter_sse, paginate, terminal_result
from combadge.artifacts import UnsafeArtifact, extract_source, validate_manifest
from combadge.config import AgentConfig
from combadge.live import deliver_agent_notifications
from combadge.service import AgentService, CapacityError, ConfirmationError
from combadge.store import Store


def config(root: Path, **changes) -> AgentConfig:
    value = AgentConfig("gpt-6-astra", 3, 30, 24, 8787, root, root / "db", root / "sock", root / "pid", root / "tasks", root / "oa", root / "gh", root / "install", root / "ui", None, "http://openai", "http://github")
    value = replace(value, **changes); value.initialize(); return value


class FakeAgents:
    def __init__(self):
        self.sessions = {}; self.turns = {}; self.cancelled = []; self.deleted = []; self.created = 0; self.artifact_data = {}
    def create_session(self, **kwargs):
        self.created += 1; sid = f"sess-{self.created}"; self.sessions[sid] = {"id": sid, "status": "in_progress", "metadata": {"combadge_installation": kwargs["installation_id"], "combadge_task": kwargs["task_id"]}}; return self.sessions[sid]
    def get_session(self, sid): return self.sessions[sid]
    def list_sessions(self, installation_id, cursor=None): return {"data": list(self.sessions.values()), "has_more": False}
    def list_history(self, sid, cursor=None): return {"data": self.turns.get(sid, []), "has_more": False}
    def steer(self, sid, message): return {}
    def cancel(self, sid):
        self.cancelled.append(sid)
        self.sessions[sid]["status"] = "idle"
        self.turns[sid] = [{"object": "agent.session.turn", "status": "cancelled", "error": {"message": "cancelled"}}]
    def delete_session(self, sid): self.deleted.append(sid)
    def list_artifacts(self, sid, cursor=None): return {"data": [{"id": key, "name": key} for key in self.artifact_data]}
    def download_artifact(self, sid, aid, **kwargs): return self.artifact_data[aid]


class ProtocolTests(unittest.TestCase):
    def test_sse_multiline_and_done(self):
        stream = io.BytesIO(b"event: delta\ndata: {\"x\":\ndata: 1}\n\ndata: [DONE]\n\n")
        self.assertEqual(list(iter_sse(stream)), [("delta", {"x": 1})])

    def test_pagination(self):
        pages = {None: {"data": [{"id": 1}], "has_more": True, "last_id": "a"}, "a": {"data": [{"id": 2}], "has_more": False}}
        self.assertEqual([x["id"] for x in paginate(lambda cursor: pages[cursor])], [1, 2])

    def test_idle_is_not_completion(self):
        self.assertIsNone(terminal_result({"status": "idle"}, []))
        result = terminal_result({}, [{"object": "agent.session.turn", "status": "completed", "final_output": "done", "usage": {"total_tokens": 4}}])
        self.assertEqual(result, ("completed", "done", {"total_tokens": 4}))


class ArtifactTests(unittest.TestCase):
    def zip(self, values):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for name, value in values.items(): archive.writestr(name, value)
        return out.getvalue()

    def test_safe_extract(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = extract_source(self.zip({"src/app.py": "ok"}), Path(tmp))
            self.assertEqual(files, ["src/app.py"]); self.assertEqual((Path(tmp) / "src/app.py").read_text(), "ok")

    def test_rejects_traversal_and_git(self):
        for name in ("../escape", "x/.git/config"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(UnsafeArtifact): extract_source(self.zip({name: "bad"}), Path(tmp))

    def test_rejects_symlink(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            info = zipfile.ZipInfo("link"); info.external_attr = 0o120777 << 16; archive.writestr(info, "target")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(UnsafeArtifact): extract_source(out.getvalue(), Path(tmp))

    def test_manifest(self):
        manifest = {k: [] if k in {"test_results", "entry_points"} else "x" for k in ("repository_name", "description", "test_results", "entry_points", "final_summary")}
        self.assertEqual(validate_manifest(json.dumps(manifest).encode()), manifest)
        with self.assertRaises(UnsafeArtifact): validate_manifest(b"{}")


class StoreAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); self.cfg = config(self.root); self.now = [1000.0]; self.store = Store(self.cfg.database, clock=lambda: self.now[0]); self.agents = FakeAgents()
        self.service = AgentService(self.cfg, self.store, self.agents, clock=lambda: self.now[0])
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def start(self, prompt="build it"):
        prepared = self.service.prepare_start(prompt); return self.service.confirm_start(prepared["confirmation"])

    def test_confirmation_is_single_use(self):
        item = self.service.prepare_start("x"); self.service.confirm_start(item["confirmation"])
        with self.assertRaises(ConfirmationError): self.service.confirm_start(item["confirmation"])

    def test_concurrency_limit(self):
        self.service.config = replace(self.cfg, max_active=1); self.start()
        with self.assertRaises(CapacityError): self.service.prepare_start("second")

    def test_timeout_cancels_and_notifies_once(self):
        task = self.start(); self.now[0] = task["deadline"]
        self.service.reconcile(task["id"]); self.service.reconcile(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "timed_out")
        self.assertEqual(self.agents.cancelled, [task["session_id"]]); self.assertEqual(len(self.store.pending_notifications()), 1)

    def test_cancel_waits_for_provider_then_deletes_session(self):
        task = self.start(); prepared = self.service.prepare_cancel(task["id"])
        result = self.service.confirm_cancel(prepared["confirmation"])
        self.assertEqual(result["status"], "cancelling")
        self.service.reconcile(task["id"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")
        self.assertIn(task["session_id"], self.agents.deleted)

    def test_completed_capture_and_cleanup(self):
        source = io.BytesIO()
        with zipfile.ZipFile(source, "w") as z: z.writestr("main.py", "print('ok')")
        manifest = {"repository_name": "demo", "description": "d", "test_results": "pass", "entry_points": ["main.py"], "final_summary": "done"}
        self.agents.artifact_data = {"source.zip": source.getvalue(), "manifest.json": json.dumps(manifest).encode()}
        task = self.start(); self.agents.sessions[task["session_id"]]["status"] = "idle"
        self.agents.turns[task["session_id"]] = [{"object": "agent.session.turn", "status": "completed", "final_output": "done", "usage": {"total_tokens": 9}}]
        self.service.reconcile(task["id"]); saved = self.store.get_task(task["id"])
        self.assertEqual(saved["status"], "completed"); self.assertEqual(saved["manifest"]["repository_name"], "demo"); self.assertIn(task["session_id"], self.agents.deleted)
        self.now[0] = saved["retention_deadline"]; self.service.cleanup(); self.assertIsNone(self.store.get_task(task["id"])); self.assertFalse((self.cfg.artifact_dir / task["id"]).exists())

    def test_recovery_attaches_orphan_session(self):
        self.store.create_task("CDX-TEST", "x", 5000)
        self.agents.sessions["orphan"] = {"id": "orphan", "status": "in_progress", "metadata": {"combadge_installation": self.cfg.installation_id, "combadge_task": "CDX-TEST"}}
        self.service.recover(); self.assertEqual(self.store.get_task("CDX-TEST")["session_id"], "orphan")

    def test_notifications_deliver_once(self):
        self.store.notify("a", "terminal", "done"); spoken = []
        self.assertEqual(deliver_agent_notifications(self.store, spoken.append), 1); self.assertEqual(deliver_agent_notifications(self.store, spoken.append), 0); self.assertEqual(spoken, ["done"])


if __name__ == "__main__": unittest.main()
