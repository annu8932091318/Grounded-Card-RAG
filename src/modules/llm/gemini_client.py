"""Google Gemini (AI Studio) — the PRIMARY provider. REST via httpx: one endpoint, mapping readable line by line.

The agent loop uses one internal message format: tool_use / tool_result blocks (provider-neutral, the most explicit
shape for a tool loop). This adapter translates it to Gemini's generateContent format and the answer back, so the
agent loop, citation validator and output guard never know which provider answered: every answer passes the
same checks.
"""
import uuid
from types import SimpleNamespace

import httpx

from src.const.config import config
from src.modules.llm.errors import ProviderRetryable, ProviderTimeout, from_status

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_SAFETY_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def _get(block, key):
    """Blocks are dicts (our tool_result) or objects (SDK / SimpleNamespace)."""
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _to_contents(messages: list) -> list[dict]:
    names: dict[str, str] = {}  # tool_use_id -> tool name; Gemini's functionResponse needs the name
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        parts = []
        if isinstance(m["content"], str):
            parts.append({"text": m["content"]})
        else:
            for b in m["content"]:
                kind = _get(b, "type")
                if kind == "text" and _get(b, "text"):
                    parts.append({"text": _get(b, "text")})
                elif kind == "tool_use":
                    names[_get(b, "id")] = _get(b, "name")
                    # WHY resend the original part: it carries Gemini's thoughtSignature, required on later turns.
                    # A call made by Grok (fallback mid-turn) has no signature: Gemini 3 accepts this documented placeholder.
                    parts.append(_get(b, "gemini_part") or {"functionCall": {"name": _get(b, "name"), "args": _get(b, "input") or {}},
                                                            "thoughtSignature": "skip_thought_signature_validator"})
                elif kind == "tool_result":
                    parts.append({"functionResponse": {
                        "name": names.get(_get(b, "tool_use_id"), "unknown"),
                        "response": {"content": _get(b, "content"), "is_error": bool(_get(b, "is_error"))},
                    }})
                # Other block types (e.g. provider-specific thinking) are dropped: Gemini cannot read them.
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def _to_response(data: dict, model: str) -> SimpleNamespace:
    candidate = (data.get("candidates") or [{}])[0]
    blocks = []
    for part in (candidate.get("content") or {}).get("parts", []):
        if "functionCall" in part:
            fc = part["functionCall"]
            blocks.append(SimpleNamespace(type="tool_use", id=f"gem_{uuid.uuid4().hex[:12]}", name=fc["name"],
                                          input=fc.get("args") or {}, gemini_part=part))
        elif part.get("text") and not part.get("thought"):
            blocks.append(SimpleNamespace(type="text", text=part["text"]))
    blocked = candidate.get("finishReason") in _SAFETY_FINISH or bool(data.get("promptFeedback", {}).get("blockReason"))
    stop = "refusal" if blocked else ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    u = data.get("usageMetadata", {})
    cached = u.get("cachedContentTokenCount", 0)
    usage = SimpleNamespace(
        input_tokens=u.get("promptTokenCount", 0) - cached,
        output_tokens=u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),  # thinking is billed as output
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(content=blocks, stop_reason=stop, model=data.get("modelVersion", model), usage=usage)


def call(model: str, system: str, tools: list[dict], messages: list) -> SimpleNamespace:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": _to_contents(messages),
        # parametersJsonSchema accepts our JSON schemas unchanged (nullable types, anyOf).
        "tools": [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"], "parametersJsonSchema": t["input_schema"]}
            for t in tools]}],
        # WHY mode ANY: the model must always call a tool, so it can never answer in uncited plain text.
        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
        "generationConfig": {"maxOutputTokens": config.llm_max_tokens},
    }
    if config.gemini_thinking_level and model.startswith("gemini-3"):  # thinkingLevel is a Gemini 3 setting
        # WHY (measured 2026-11-14, 3.1-flash-lite): default thinking 38.6 s, "low" 9.1 s, "minimal" 2.1 s per call.
        # The agent has a 25 s budget for 3-5 calls, so thinking depth is a latency setting here.
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": config.gemini_thinking_level}
    try:
        r = httpx.post(URL.format(model=model), json=body, timeout=config.gemini_timeout_s,
                       headers={"x-goog-api-key": config.gemini_api_key})  # header, never the URL: keys stay out of logs
    except httpx.TimeoutException as e:
        raise ProviderTimeout(f"gemini {type(e).__name__}") from e
    except httpx.TransportError as e:
        raise ProviderRetryable(f"gemini {type(e).__name__}") from e
    if r.status_code != 200:
        raise from_status("gemini", r.status_code, r.text, r.headers)
    return _to_response(r.json(), model)
