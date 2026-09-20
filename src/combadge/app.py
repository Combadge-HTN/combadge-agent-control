from __future__ import annotations

import json
from typing import Any

from .service import AgentService, ServiceError


class Application:
    """Transport-neutral command dispatcher used by voice, Unix socket, and HTTP."""
    def __init__(self, service: AgentService): self.service = service

    def call(self, action: str, data: dict[str, Any]) -> Any:
        routes = {
            "prepare_start": lambda: self.service.prepare_start(data["prompt"]),
            "confirm_start": lambda: self.service.confirm_start(data["confirmation"]),
            "list": self.service.list_tasks,
            "status": lambda: self.service.status(data["task_id"]),
            "steer": lambda: self.service.steer(data["task_id"], data["message"]),
            "prepare_cancel": lambda: self.service.prepare_cancel(data["task_id"]),
            "confirm_cancel": lambda: self.service.confirm_cancel(data["confirmation"]),
            "prepare_publish": lambda: self.service.prepare_publish(data["task_id"], data["repository"], data["visibility"]),
            "confirm_publish": lambda: self.service.confirm_publish(data["confirmation"]),
        }
        if action not in routes: raise ServiceError("unknown action")
        return routes[action]()

    def request(self, value: dict[str, Any]) -> dict[str, Any]:
        try: return {"ok": True, "result": self.call(value.get("action", ""), value.get("data", {}))}
        except (ServiceError, KeyError, ValueError) as exc: return {"ok": False, "error": str(exc)}

