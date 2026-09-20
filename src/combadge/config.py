from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass(frozen=True)
class AgentConfig:
    model: str
    max_active: int
    timeout_minutes: int
    retention_hours: int
    ui_port: int
    state_dir: Path
    database: Path
    socket: Path
    pid_file: Path
    artifact_dir: Path
    openai_key_file: Path
    github_token_file: Path
    installation_id_file: Path
    ui_token_file: Path
    github_owner: str | None
    openai_base_url: str
    github_base_url: str

    @classmethod
    def from_env(cls) -> "AgentConfig":
        state = _path("COMBADGE_AGENT_STATE_DIR", Path.home() / ".local/state/combadge")
        return cls(
            model=os.environ.get("OPENAI_AGENT_MODEL", "gpt-6-astra"),
            max_active=int(os.environ.get("COMBADGE_AGENT_MAX_ACTIVE", "3")),
            timeout_minutes=int(os.environ.get("COMBADGE_AGENT_TIMEOUT_MINUTES", "30")),
            retention_hours=int(os.environ.get("COMBADGE_AGENT_RETENTION_HOURS", "24")),
            ui_port=int(os.environ.get("COMBADGE_AGENT_UI_PORT", "8787")),
            state_dir=state,
            database=_path("COMBADGE_AGENT_DATABASE", state / "agents.sqlite3"),
            socket=_path("COMBADGE_AGENT_SOCKET", state / "agents.sock"),
            pid_file=_path("COMBADGE_AGENT_PID_FILE", state / "agents.pid"),
            artifact_dir=_path("COMBADGE_AGENT_ARTIFACT_DIR", state / "tasks"),
            openai_key_file=_path("COMBADGE_OPENAI_KEY_FILE", state / "openai.key"),
            github_token_file=_path("COMBADGE_GITHUB_TOKEN_FILE", state / "github.pat"),
            installation_id_file=_path("COMBADGE_INSTALLATION_ID_FILE", state / "installation.json"),
            ui_token_file=_path("COMBADGE_AGENT_UI_TOKEN_FILE", state / "ui.token"),
            github_owner=os.environ.get("COMBADGE_GITHUB_OWNER"),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            github_base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )

    def initialize(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path, content in (
            (self.installation_id_file, json.dumps({"id": secrets.token_hex(16)})),
            (self.ui_token_file, secrets.token_urlsafe(32)),
        ):
            if not path.exists():
                path.write_text(content, encoding="utf-8")
                path.chmod(0o600)

    def read_secret(self, path: Path) -> str:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"credential file must be mode 0600: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"credential file is empty: {path}")
        return value

    @property
    def installation_id(self) -> str:
        return json.loads(self.installation_id_file.read_text(encoding="utf-8"))["id"]

