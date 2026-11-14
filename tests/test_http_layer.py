"""HTTP layer without Postgres/ChromaDB/LLM: Idempotency-Key rules + replay, request id, request_logs row,
and the log file line format."""
import json

import pytest
from fastapi.testclient import TestClient

from src.controller import chat_controller
from src.db.repositories import request_log_repo

H = {"x-api-key": "demo-key-1"}
BODY = {"customer_id": "CUST-2007", "message": "What is the foreign currency markup?"}


class FakeStore:
    """In-memory stand-in for the idempotency_keys and request_logs tables."""

    def __init__(self):
        self.keys, self.rows = {}, []

    def claim_key(self, tenant, key, method, path, body_sha, ttl_s):
        if (tenant, key) in self.keys:
            return self.keys[(tenant, key)]
        self.keys[(tenant, key)] = {"body_sha256": body_sha, "path": path, "state": "in_progress"}
        return None

    def complete_key(self, tenant, key, status_code, response):
        self.keys[(tenant, key)].update(state="completed", status_code=status_code, response=response)

    def release_key(self, tenant, key):
        self.keys.pop((tenant, key), None)


@pytest.fixture
def client(monkeypatch):
    store = FakeStore()
    for name in ("claim_key", "complete_key", "release_key"):
        monkeypatch.setattr(request_log_repo, name, getattr(store, name))
    monkeypatch.setattr(request_log_repo, "save", store.rows.append)
    calls = []

    def fake_agent(customer_id, message, tenant_id="default", conversation_id=None, on_event=None):
        calls.append(message)
        return {"trace_id": "00000000-0000-0000-0000-000000000001", "status": "answered", "customer_message": "3.5%",
                "claims": [], "citations_flat": [], "meta": {"provider": "gemini", "model": "gemini-2.5-flash",
                                                             "fallback_used": False}}
    monkeypatch.setattr(chat_controller, "run_agent", fake_agent)
    from src.app import app

    with TestClient(app) as c:
        c.store, c.calls = store, calls
        yield c


def test_post_without_idempotency_key_is_rejected(client):
    r = client.post("/api/v1/chat", json=BODY, headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert client.calls == []


def test_invalid_idempotency_key_is_rejected(client):
    r = client.post("/api/v1/chat", json=BODY, headers={**H, "Idempotency-Key": "bad key!"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"


def test_same_key_same_body_is_replayed_not_rerun(client):
    h = {**H, "Idempotency-Key": "chat-test-0001"}
    first = client.post("/api/v1/chat", json=BODY, headers=h)
    second = client.post("/api/v1/chat", json=BODY, headers=h)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert second.headers["idempotent-replayed"] == "true" and len(client.calls) == 1
    assert first.headers["x-request-id"] == "chat-test-0001"  # the key is the requestId


def test_same_key_different_body_is_422(client):
    h = {**H, "Idempotency-Key": "chat-test-0002"}
    client.post("/api/v1/chat", json=BODY, headers=h)
    r = client.post("/api/v1/chat", json={**BODY, "message": "something else"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_request_log_row_records_source_and_model(client):
    client.post("/api/v1/chat", json=BODY, headers={**H, "Idempotency-Key": "chat-test-0003", "user-agent": "PostmanRuntime/7.39"})
    row = next(r for r in client.store.rows if r["request_id"] == "chat-test-0003")
    assert (row["source"], row["provider"], row["model_used"], row["status_code"]) == ("postman", "gemini", "gemini-2.5-flash", 200)
    assert row["api_key_hash"] and "demo-key-1" not in json.dumps(row)


def test_get_needs_no_idempotency_key(client):
    assert client.get("/api/v1/health").status_code == 200


def test_log_file_line_format(client):
    from src.modules.telemetry.logger import file_handler

    client.post("/api/v1/chat", json=BODY, headers={**H, "Idempotency-Key": "chat-test-0004"})
    lines = file_handler.path_for_today().read_text(encoding="utf-8").splitlines()
    entry = json.loads(next(line for line in reversed(lines) if "chat-test-0004" in line and "request_finished" in line))
    assert set(entry) >= {"date", "type", "message"} and entry["type"] == "info"
    parts = [p.strip() for p in entry["message"].split(" | ")]
    assert parts[0] == "Grounded-Card-RAG" and parts[1] == "src/middleware" and parts[2] == "request_context_middleware.py"
    assert parts[4] == "requestId: chat-test-0004" and parts[5].startswith("message: request_finished")


def test_stream_runs_agent_and_records_model(client):
    """Regression (found in the live run): the worker thread's context variable shadowed the request ctx -> error event."""
    r = client.post("/api/v1/chat/stream", json=BODY, headers={**H, "Idempotency-Key": "stream-test-0001"})
    events = [line for line in r.text.splitlines() if line.startswith("event:")]
    assert "event: final" in events and "event: error" not in events
    row = next(x for x in client.store.rows if x["request_id"] == "stream-test-0001")
    assert row["model_used"] == "gemini-2.5-flash"  # recorded after the body finished streaming
