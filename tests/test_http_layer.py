"""HTTP layer without Postgres/ChromaDB/LLM: Idempotency-Key rules + replay, request id, the req_res_log row,
and the log file line format."""
import json

import pytest
from fastapi.testclient import TestClient

from src.controller import chat_controller
from src.db.repositories import req_res_log_repo

H = {"x-api-key": "demo-key-1"}
BODY = {"customer_id": "CUST-2007", "message": "What is the foreign currency markup? My card is 4111 1111 1111 1111"}


class FakeLog:
    """In-memory stand-in for req_res_log (claim -> upsert -> replay)."""

    def __init__(self):
        self.by_key, self.rows = {}, []

    def claim(self, tenant, key, method, path, body_sha, ttl_s):
        row = self.by_key.get((tenant, key))
        if row and row["state"] != "failed":
            return row
        self.by_key[(tenant, key)] = {"state": "in_progress", "path": path, "request_body_sha256": body_sha,
                                      "status_code": None, "response_body": None}
        return None

    def upsert(self, row):
        self.rows.append(row)
        self.by_key.setdefault((row["tenant_id"], row["idempotency_key"]), {}).update(
            state=row["state"], path=row["path"], request_body_sha256=row.get("request_body_sha256"),
            status_code=row["status_code"], response_body=row["response_body"])

    def record_replay(self, tenant, key):
        self.by_key[(tenant, key)]["replays"] = self.by_key[(tenant, key)].get("replays", 0) + 1

    def mark_failed(self, tenant, key):
        self.by_key[(tenant, key)]["state"] = "failed"


@pytest.fixture
def client(monkeypatch):
    store = FakeLog()
    for name in ("claim", "upsert", "record_replay", "mark_failed"):
        monkeypatch.setattr(req_res_log_repo, name, getattr(store, name))
    calls = []

    def fake_agent(customer_id, message, tenant_id="default", conversation_id=None, on_event=None):
        calls.append(message)
        return {"trace_id": "00000000-0000-0000-0000-000000000001", "status": "answered", "customer_message": "3.5%",
                "claims": [], "citations_flat": [], "meta": {"provider": "gemini", "model": "gemini-2.5-flash"},
                "_internal": {"provider": "gemini", "model_used": "gemini-2.5-flash", "agent_status": "answered",
                              "input_tokens": 1200, "output_tokens": 80, "llm_calls": 2, "llm_failures": 1,
                              "models_failed": ["gemini-3.5-flash-lite"], "request_message": "masked text"}}
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
    assert client.store.by_key[("default", "chat-test-0001")]["replays"] == 1
    assert len([r for r in client.store.rows if r["idempotency_key"] == "chat-test-0001"]) == 1  # replay: no new row


def test_same_key_different_body_is_422(client):
    h = {**H, "Idempotency-Key": "chat-test-0002"}
    client.post("/api/v1/chat", json=BODY, headers=h)
    r = client.post("/api/v1/chat", json={**BODY, "message": "something else"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_req_res_log_row_has_source_model_usage_and_masked_body(client):
    client.post("/api/v1/chat", json=BODY, headers={**H, "Idempotency-Key": "chat-test-0003", "user-agent": "PostmanRuntime/7.39"})
    row = next(r for r in client.store.rows if r["idempotency_key"] == "chat-test-0003")
    assert (row["source"], row["provider"], row["model_used"], row["status_code"]) == ("postman", "gemini", "gemini-2.5-flash", 200)
    assert (row["input_tokens"], row["llm_failures"], row["models_failed"]) == (1200, 1, ["gemini-3.5-flash-lite"])
    assert row["response_time_ms"] >= 0 and row["response_body"]["status"] == "answered"
    dumped = json.dumps(row, default=str)
    assert "demo-key-1" not in dumped and "4111" not in dumped and "CUST-2007" not in dumped  # key, PII, raw id never stored
    assert row["request_body"]["message"] == "masked text"


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


def test_stream_runs_agent_and_stores_final_answer_for_replay(client):
    """Regression (found in the live run): the worker thread's context variable shadowed the request ctx -> error event."""
    h = {**H, "Idempotency-Key": "stream-test-0001"}
    r = client.post("/api/v1/chat/stream", json=BODY, headers=h)
    events = [line for line in r.text.splitlines() if line.startswith("event:")]
    assert "event: final" in events and "event: error" not in events
    row = next(x for x in client.store.rows if x["idempotency_key"] == "stream-test-0001")
    assert row["model_used"] == "gemini-2.5-flash" and row["response_body"]["status"] == "answered"
    replay = client.post("/api/v1/chat/stream", json=BODY, headers=h)
    assert replay.headers["idempotent-replayed"] == "true" and replay.json()["status"] == "answered"
