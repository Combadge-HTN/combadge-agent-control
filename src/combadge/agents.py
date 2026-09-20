from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterator


class APIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def iter_sse(stream: BinaryIO) -> Iterator[tuple[str, dict[str, Any]]]:
    event = "message"
    data: list[str] = []
    for raw in stream:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                value = "\n".join(data)
                if value != "[DONE]":
                    yield event, json.loads(value)
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data and '\n'.join(data) != '[DONE]':
        yield event, json.loads("\n".join(data))


@dataclass
class AgentsClient:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    opener: Any = urllib.request.urlopen

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, stream: bool = False, extra_headers: dict[str, str] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.api_key}", "OpenAI-Beta": "agents=v1", "Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json", **(extra_headers or {})},
        )
        try:
            response = self.opener(request, timeout=65)
            if stream: return response
            with response:
                content = response.read()
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as exc:
            try: detail = exc.read().decode("utf-8", "replace")
            finally: exc.close()
            raise APIError(f"Agents API {exc.code}: {detail[:500]}", exc.code) from exc

    def create_session(self, *, task_id: str, installation_id: str, prompt: str, model: str) -> dict[str, Any]:
        return self._request("POST", "/agents/sessions", {
            "environment": {"type": "openai_hosted", "network": {"access": "disabled"}},
            "agent": {"model": model, "tools": [{"type": "web_search"}]},
            "metadata": {"combadge_installation": installation_id, "combadge_task": task_id},
            "input": prompt + "\n\nRequired durable outputs: write the source archive to /workspace/outputs/source.zip and the manifest to /workspace/outputs/manifest.json. The archive must not contain repository metadata. manifest.json must contain repository_name, description, test_results, entry_points, and final_summary. Files outside /workspace/outputs are not delivered to the caller.",
        }, extra_headers={"Idempotency-Key": f"combadge-{installation_id}-{task_id}"})

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/agents/sessions/{urllib.parse.quote(session_id)}")

    def list_sessions(self, *, installation_id: str, cursor: str | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode({"order": "desc", "limit": 100, **({"after": cursor} if cursor else {})})
        page = self._request("GET", f"/agents/sessions?{query}")
        page["data"] = [s for s in page.get("data", []) if s.get("metadata", {}).get("combadge_installation") == installation_id]
        return page

    def list_history(self, session_id: str, cursor: str | None = None) -> dict[str, Any]:
        suffix = "?" + urllib.parse.urlencode({"order": "asc", **({"after": cursor} if cursor else {})})
        return self._request("GET", f"/agents/sessions/{urllib.parse.quote(session_id)}/turns{suffix}")

    def stream(self, session_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
        with self._request("GET", f"/agents/sessions/{urllib.parse.quote(session_id)}/events", stream=True) as response:
            yield from iter_sse(response)

    def steer(self, session_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", f"/agents/sessions/{urllib.parse.quote(session_id)}/events", {"events": [{"type": "agent.session.input.message", "input": [{"role": "user", "content": [{"type": "input_text", "text": message}]}]}]})

    def cancel(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/agents/sessions/{urllib.parse.quote(session_id)}/events", {"events": [{"type": "agent.session.input.cancel"}]})

    def list_artifacts(self, session_id: str, cursor: str | None = None) -> dict[str, Any]:
        suffix = '?' + urllib.parse.urlencode({'after': cursor}) if cursor else ''
        return self._request("GET", f"/agents/sessions/{urllib.parse.quote(session_id)}/artifacts{suffix}")

    def download_artifact(self, session_id: str, artifact_id: str, *, max_bytes: int = 100 * 1024 * 1024) -> bytes:
        response = self._request("GET", f"/agents/sessions/{urllib.parse.quote(session_id)}/artifacts/{urllib.parse.quote(artifact_id)}/content", stream=True)
        chunks, total = [], 0
        with response:
            while True:
                chunk = response.read(min(65536, max_bytes + 1 - total))
                if not chunk: break
                total += len(chunk)
                if total > max_bytes: raise APIError('artifact exceeds download size limit')
                chunks.append(chunk)
        return b''.join(chunks)

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/agents/sessions/{urllib.parse.quote(session_id)}")


def paginate(fetch: Any) -> Iterator[dict[str, Any]]:
    cursor = None
    while True:
        page = fetch(cursor)
        yield from page.get("data", [])
        if not page.get("has_more"): break
        cursor = page.get("last_id")
        if not cursor: break


def terminal_result(session: dict[str, Any], history: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any]] | None:
    """Return status, summary, usage only when a turn has a terminal result."""
    terminal = {"completed", "failed", "cancelled", "expired"}
    turns = [item for item in history if item.get("object") == "agent.session.turn" or "status" in item]
    if not turns or turns[-1].get("status") not in terminal:
        return None
    turn = turns[-1]
    status = "completed" if turn["status"] == "completed" else turn["status"]
    error = turn.get("error") or {}
    summary = turn.get("final_output") or turn.get("output_text") or (error.get("message") if isinstance(error, dict) else error) or session.get("final_output") or f"Agent task {status}."
    if isinstance(summary, dict): summary = summary.get("text") or json.dumps(summary)
    return status, str(summary), turn.get("usage") or session.get("usage") or {}
