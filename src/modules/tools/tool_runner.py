"""Wraps the supplied tools: customer binding, timeout, retry + backoff, circuit breaker, redaction, idempotency.
This is the "what the customer sees when upstream fails" layer (spec §9.3)."""
from src.const.categories import CATEGORIES, QUEUE_NAMES
from src.const.config import config
from src.helper.async_utils import CircuitBreaker, TimeoutExceeded, backoff_delay, sleep, with_timeout
from src.helper.hash import customer_hash, idempotency_key
from src.modules.guardrails.redact import redact_account
from src.modules.telemetry.logger import log
from src.modules.tools.errors import CircuitOpen, CustomerNotFound, InvalidCategory, UpstreamTimeout
from src.modules.tools.get_account_state import get_account_state
from src.modules.tools.raise_service_request import raise_service_request

account_breaker = CircuitBreaker("get_account_state", config.circuit_fails_to_open, config.circuit_open_s)
sr_breaker = CircuitBreaker("raise_service_request", config.circuit_fails_to_open, config.circuit_open_s)

ACCOUNT_STATE_UNAVAILABLE = {"error": "ACCOUNT_STATE_UNAVAILABLE",
                             "instruction": "Do not guess any account value. Tell the customer honestly."}


def run_get_account_state(turn) -> dict:
    """turn: TurnContext. customer_id comes from turn (request context), never from the model.
    Returns redacted state, or ACCOUNT_STATE_UNAVAILABLE after retries. CustomerNotFound propagates (-> 404)."""
    if turn.raw_account is not None:  # WHY: one fetch per turn; the eligibility tool reuses it
        return turn.account_state
    attempts = 1 + config.tool_max_retries
    for attempt in range(1, attempts + 1):
        with turn.trace.span("tool.get_account_state", attempt=attempt) as attrs:
            if not account_breaker.allow():
                attrs["failed"], attrs["error"] = True, "circuit_open"
                break
            try:
                raw = with_timeout(get_account_state, config.tool_timeout_s, turn.customer_id)
                account_breaker.success()
                turn.raw_account = raw  # raw stays server-side for decision/ rules only
                turn.account_state = redact_account(raw)
                log("account_state_loaded", attempt=attempt, trace_id=turn.trace.trace_id)
                return turn.account_state
            except CustomerNotFound:
                account_breaker.success()  # the service answered; the id is just unknown
                raise
            except (UpstreamTimeout, TimeoutExceeded) as e:
                log("account_state_timeout", level="warning", attempt=attempt, trace_id=turn.trace.trace_id)
                account_breaker.failure()
                attrs["failed"], attrs["error"] = True, type(e).__name__
        if attempt < attempts:
            sleep(backoff_delay(attempt))
    turn.trace.flag("tool_timeout")
    turn.account_unavailable = True
    log("tool_failed", level="warning", tool="get_account_state", trace_id=turn.trace.trace_id, attempts=attempts)
    return ACCOUNT_STATE_UNAVAILABLE


def run_raise_service_request(turn, category: str, summary: str) -> dict:
    """No blind retry: the contract has no idempotency key, so a timed-out call may already have created a request."""
    from src.db.repositories import service_request_repo as repo  # WHY local: unit tests patch / skip the DB

    if category not in CATEGORIES:
        turn.invalid_category_count += 1
        if turn.invalid_category_count > 1:
            category = "GENERAL"  # agent bug twice -> route to a human rather than guess
            turn.trace.flag("invalid_category_routed_general")
        else:
            return {"error": "InvalidCategory", "allowed": QUEUE_NAMES,
                    "instruction": "Pick exactly one allowed queue; if unsure use GENERAL."}

    if turn.raw_account is None and not turn.account_unavailable:
        # WHY: never open a request for a customer that does not exist — CustomerNotFound propagates (-> 404).
        # Found by the live API test: static mode raised an SR for CUST-9999.
        run_get_account_state(turn)

    key = idempotency_key(turn.customer_id, category, summary, turn.conversation_id or turn.trace.trace_id)
    existing = repo.find_recent(key)
    if existing and existing["state"] == "created":
        result = {k: existing[k] for k in ("request_id", "queue", "sla_hours")}
        return _remember(turn, {**result, "sla": CATEGORIES[result["queue"]]["sla"], "deduplicated": True})

    repo.insert_pending(key, customer_hash(turn.customer_id), category)
    with turn.trace.span("tool.raise_service_request", category=category) as attrs:
        if not sr_breaker.allow():
            attrs["failed"] = True
            repo.mark(key, "failed")
            return {"error": "REQUEST_NOT_SUBMITTED", "instruction": "Tell the customer it was not submitted."}
        try:
            result = with_timeout(raise_service_request, config.tool_timeout_s, turn.customer_id, category, summary)
            sr_breaker.success()
        except InvalidCategory:
            repo.mark(key, "failed")
            return {"error": "InvalidCategory", "allowed": QUEUE_NAMES}
        except (TimeoutExceeded, CircuitOpen) as e:
            sr_breaker.failure()
            attrs["failed"], attrs["error"] = True, type(e).__name__
            repo.mark(key, "unknown")
            turn.trace.flag("service_request_unknown")
            return {"error": "REQUEST_STATUS_UNKNOWN",
                    "instruction": "Say: I couldn't confirm your request was submitted. Please don't assume it was."}
    repo.mark(key, "created", result)
    log("service_request_raised", request_id=result["request_id"], queue=result["queue"], trace_id=turn.trace.trace_id)
    attrs["request_id"] = result["request_id"]
    return _remember(turn, {**result, "sla": CATEGORIES[category]["sla"]})


def _remember(turn, result: dict) -> dict:
    turn.service_requests[result["request_id"]] = result
    return result
