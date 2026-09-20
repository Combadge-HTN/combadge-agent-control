from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class ControlClient:
    def __init__(self, socket_path: Path): self.socket_path = socket_path
    def call(self, action: str, **data: Any) -> Any:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(self.socket_path)); sock.sendall(json.dumps({"action": action, "data": data}).encode() + b"\n")
            stream = sock.makefile("rb"); result = json.loads(stream.readline())
        if not result["ok"]: raise RuntimeError(result["error"])
        return result["result"]

