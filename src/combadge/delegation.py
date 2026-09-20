from __future__ import annotations

from .client import ControlClient


class CodexDelegationTools:
    """Voice-tool facade. The returned confirmation token is opaque and single-use."""
    def __init__(self, client: ControlClient): self.client = client
    def prepare_codex_task(self, prompt: str): return self.client.call("prepare_start", prompt=prompt)
    def confirm_codex_task(self, confirmation: str): return self.client.call("confirm_start", confirmation=confirmation)
    def list_codex_tasks(self): return self.client.call("list")
    def get_codex_task_status(self, task_id: str): return self.client.call("status", task_id=task_id)
    def steer_codex_task(self, task_id: str, message: str): return self.client.call("steer", task_id=task_id, message=message)
    def prepare_cancel_codex_task(self, task_id: str): return self.client.call("prepare_cancel", task_id=task_id)
    def confirm_cancel_codex_task(self, confirmation: str): return self.client.call("confirm_cancel", confirmation=confirmation)
    def prepare_publish_codex_task(self, task_id: str, repository: str, visibility: str):
        return self.client.call("prepare_publish", task_id=task_id, repository=repository, visibility=visibility)
    def confirm_publish_codex_task(self, confirmation: str): return self.client.call("confirm_publish", confirmation=confirmation)

    @staticmethod
    def schemas() -> list[dict]:
        return [
            {"name": "prepare_codex_task", "description": "Prepare a hosted coding task for spoken confirmation", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
            {"name": "confirm_codex_task", "description": "Confirm a prepared coding task", "parameters": {"type": "object", "properties": {"confirmation": {"type": "string"}}, "required": ["confirmation"]}},
            {"name": "list_codex_tasks", "description": "List coding tasks", "parameters": {"type": "object", "properties": {}}},
            {"name": "get_codex_task_status", "description": "Get coding task status", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
            {"name": "steer_codex_task", "description": "Send guidance to a running coding task", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, "message": {"type": "string"}}, "required": ["task_id", "message"]}},
            {"name": "prepare_cancel_codex_task", "description": "Prepare cancellation for confirmation", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
            {"name": "confirm_cancel_codex_task", "description": "Confirm cancellation", "parameters": {"type": "object", "properties": {"confirmation": {"type": "string"}}, "required": ["confirmation"]}},
            {"name": "prepare_publish_codex_task", "description": "Prepare publication with explicit repository and visibility", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}, "repository": {"type": "string"}, "visibility": {"enum": ["public", "private"]}}, "required": ["task_id", "repository", "visibility"]}},
            {"name": "confirm_publish_codex_task", "description": "Confirm GitHub publication", "parameters": {"type": "object", "properties": {"confirmation": {"type": "string"}}, "required": ["confirmation"]}},
        ]

