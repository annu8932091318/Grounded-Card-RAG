"""Supplied tool contracts + toolRunner failure handling (forced timeout, invalid category, binding, redaction)."""
import re
import types

import pytest

from src.const.config import config
from src.modules.tools import tool_runner
from src.modules.tools.errors import CustomerNotFound, InvalidCategory, UpstreamTimeout
from src.modules.tools.get_account_state import get_account_state
from src.modules.tools.raise_service_request import raise_service_request


def test_get_account_state_returns_record():
    assert get_account_state("CUST-2003")["open_dispute"] is True


def test_get_account_state_unknown_customer():
    with pytest.raises(CustomerNotFound):
        get_account_state("CUST-9999")


def test_get_account_state_forced_timeout(monkeypatch):
    monkeypatch.setattr(config, "tool_fail_rate", 1.0)
    with pytest.raises(UpstreamTimeout):
        get_account_state("CUST-2000")


def test_raise_service_request_contract():
    r = raise_service_request("CUST-2000", "DISPUTES", "card used at unknown shop")
    assert re.fullmatch(r"SR-[0-9A-F]{8}", r["request_id"]) and r["queue"] == "DISPUTES"
    assert isinstance(r["sla_hours"], int)


def test_raise_service_request_rejects_unknown_category():
    with pytest.raises(InvalidCategory):
        raise_service_request("CUST-2000", "TRAVEL", "travel insurance")


def test_runner_forced_timeout_degrades_after_retries(monkeypatch, make_turn):
    monkeypatch.setattr(config, "tool_fail_rate", 1.0)
    turn = make_turn("CUST-2000")
    assert tool_runner.run_get_account_state(turn) is tool_runner.ACCOUNT_STATE_UNAVAILABLE
    attempts = [s for s in turn.trace.spans if s["name"] == "tool.get_account_state"]
    assert len(attempts) == 1 + config.tool_max_retries
    assert "tool_timeout" in turn.trace.flags and turn.account_unavailable and turn.raw_account is None


def test_runner_redacts_and_binds_customer(make_turn):
    turn = make_turn("CUST-2006")
    state = tool_runner.run_get_account_state(turn)
    assert state["customer_id"] == "CUST-2006"  # bound from the turn, the model passes no id
    assert "behavioural_score" not in state and "name" not in state
    assert turn.raw_account["behavioural_score"] == 720  # raw stays server-side for rules only


def test_runner_invalid_category_repair_then_general(monkeypatch, make_turn):
    fake_repo = types.SimpleNamespace(find_recent=lambda k: None, insert_pending=lambda *a: None, mark=lambda *a: None)
    monkeypatch.setitem(__import__("sys").modules, "src.db.repositories.service_request_repo", fake_repo)
    import src.db.repositories as repos
    monkeypatch.setattr(repos, "service_request_repo", fake_repo, raising=False)

    turn = make_turn("CUST-2000")
    first = tool_runner.run_raise_service_request(turn, "TRAVEL", "travel insurance")
    assert first["error"] == "InvalidCategory" and "GENERAL" in first["allowed"]
    second = tool_runner.run_raise_service_request(turn, "TRAVEL", "travel insurance")
    assert second["queue"] == "GENERAL" and second["request_id"] in turn.service_requests
