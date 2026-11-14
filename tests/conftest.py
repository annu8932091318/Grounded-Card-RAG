"""Shared fixtures. Unit tests need neither Postgres nor an API key."""
import json

import pytest

from src.const.config import config
from src.helper.prompt_loader import load_prompt
from src.modules.agent.agent import TurnContext
from src.modules.telemetry.tracer import Trace


@pytest.fixture
def accounts() -> dict:
    return json.loads((config.data_dir / "accounts.json").read_text(encoding="utf-8"))


@pytest.fixture
def make_turn():
    def _make(customer_id: str = "CUST-2000") -> TurnContext:
        return TurnContext(customer_id, "default", None, Trace("default", customer_id, load_prompt()))
    return _make


@pytest.fixture(autouse=True)
def deterministic_tools(monkeypatch):
    """No random tool failures and no artificial latency unless a test asks for them."""
    from src.modules.tools import get_account_state, tool_runner

    monkeypatch.setattr(config, "tool_fail_rate", 0.0)
    monkeypatch.setattr(get_account_state, "LATENCY_S", 0.0)
    monkeypatch.setattr(tool_runner, "sleep", lambda _s: None)
    tool_runner.account_breaker.success()
    tool_runner.sr_breaker.success()
    tool_runner._recent_requests.clear()


@pytest.fixture(autouse=True)
def no_database_writes(monkeypatch):
    """Unit tests never write req_res_log rows (script runs persist from the tracer) or use the semantic cache."""
    from src.db.repositories import req_res_log_repo

    monkeypatch.setattr(req_res_log_repo, "upsert", lambda row: None)
    monkeypatch.setattr(config, "answer_cache_ttl_s", 0)
