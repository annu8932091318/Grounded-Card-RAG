"""The structured response (spec §10.3): pydantic model for validation + JSON schema shown to the LLM.
customer_message is NOT in here — the controller renders it from validated claims."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from src.const.statuses import STATUSES

Status = Literal[tuple(STATUSES)]  # type: ignore[valid-type]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyCitation(_Strict):
    kind: Literal["policy"]
    doc_id: str
    section: str
    quote: str | None = None


class AccountCitation(_Strict):
    kind: Literal["account"]
    field: str


class ToolCitation(_Strict):
    kind: Literal["tool"]
    tool: str
    request_id: str | None = None


class MissCitation(_Strict):
    kind: Literal["retrieval_miss"]
    query: str


Citation = Annotated[Union[PolicyCitation, AccountCitation, ToolCitation, MissCitation], Field(discriminator="kind")]


class Claim(_Strict):
    id: str
    type: Literal["policy_fact", "account_fact", "decision", "next_step", "gap"]
    text: str = Field(min_length=1, max_length=500)
    citations: list[Citation]


class Decision(_Strict):
    outcome: Literal["declined", "referred", "none"]
    reason_code: str | None = None


class Routing(_Strict):
    queue: str
    request_id: str | None = None
    sla: str | None = None


class ConflictNoted(_Strict):
    docs: list[str]
    prevails: str


class RespondPayload(_Strict):
    status: Status
    language: str = "en"
    claims: list[Claim] = Field(min_length=1)
    decision: Decision
    routing: Routing | None = None
    unanswered: list[str] = []
    conflicts_noted: list[ConflictNoted] = []


# ---- JSON schema for the `respond` tool. Hand-written (not generated) so it can be read line by line and
# meets strict-mode rules: every object has additionalProperties:false and lists all properties as required;
# optional values are expressed as nullable types.
_S = {"type": "string"}
_NS = {"type": ["string", "null"]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


RESPOND_JSON_SCHEMA = _obj({
    "status": {"type": "string", "enum": STATUSES},
    "language": _S,
    "claims": {"type": "array", "items": _obj({
        "id": _S,
        "type": {"type": "string", "enum": ["policy_fact", "account_fact", "decision", "next_step", "gap"]},
        "text": _S,
        "citations": {"type": "array", "items": {"anyOf": [
            _obj({"kind": {"type": "string", "enum": ["policy"]}, "doc_id": _S, "section": _S, "quote": _NS}),
            _obj({"kind": {"type": "string", "enum": ["account"]}, "field": _S}),
            _obj({"kind": {"type": "string", "enum": ["tool"]}, "tool": _S, "request_id": _NS}),
            _obj({"kind": {"type": "string", "enum": ["retrieval_miss"]}, "query": _S}),
        ]}},
    })},
    "decision": _obj({
        "outcome": {"type": "string", "enum": ["declined", "referred", "none"]},
        "reason_code": _NS,
    }),
    "routing": {"anyOf": [_obj({"queue": _S, "request_id": _NS, "sla": _NS}), {"type": "null"}]},
    "unanswered": {"type": "array", "items": _S},
    "conflicts_noted": {"type": "array", "items": _obj({"docs": {"type": "array", "items": _S}, "prevails": _S})},
})
