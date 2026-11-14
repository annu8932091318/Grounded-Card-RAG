"""FALLBACK provider: xAI Grok or Groq (used when every Gemini model is down, rate-limited, hung or misconfigured).

Both xAI (api.x.ai) and Groq (api.groq.com/openai) are OpenAI-compatible (POST {GROK_BASE_URL}/chat/completions). This adapter maps the internal
tool_use / tool_result message format to OpenAI chat messages and maps the answer back, exactly like
gemini_client does for Gemini, so the rest of the agent is unchanged whichever provider answers.
"""
import json
import uuid
from types import SimpleNamespace

import httpx

from src.const.config import config
from src.modules.llm.errors import ProviderRetryable, ProviderTimeout, from_status


def provider_name() -> str:
    """'groq' when GROK_BASE_URL points at Groq (keys start with gsk_), else 'grok' (xAI, keys start with xai-).
    Both speak the OpenAI chat-completions protocol, so one adapter serves either."""
    return "groq" if "groq.com" in config.grok_base_url else "grok"


def _get(block, key):
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _to_messages(system: str, messages: list) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = " ".join(_get(b, "text") for b in content if _get(b, "type") == "text" and _get(b, "text"))
            calls = [{"id": _get(b, "id"), "type": "function",
                      "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})}}
                     for b in content if _get(b, "type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # user turn: tool results become role=tool messages (one per call), plain text stays a user message
        for b in content:
            kind = _get(b, "type")
            if kind == "tool_result":
                body = str(_get(b, "content"))
                out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                            "content": ("ERROR: " + body) if _get(b, "is_error") else body})
            elif kind == "text" and _get(b, "text"):
                out.append({"role": "user", "content": _get(b, "text")})
    return out


def _to_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


def _to_response(data: dict, model: str) -> SimpleNamespace:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []
    if msg.get("content"):
        blocks.append(SimpleNamespace(type="text", text=msg["content"]))
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_unparseable_arguments": (fn.get("arguments") or "")[:500]}  # -> schema error -> repair turn
        blocks.append(SimpleNamespace(type="tool_use", id=call.get("id") or f"grok_{uuid.uuid4().hex[:12]}",
                                      name=fn.get("name"), input=args))
    refused = choice.get("finish_reason") == "content_filter" or bool(msg.get("refusal"))
    stop = "refusal" if refused else ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    usage = SimpleNamespace(input_tokens=(u.get("prompt_tokens") or 0) - cached,
                            output_tokens=u.get("completion_tokens") or 0,
                            cache_read_input_tokens=cached, cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop, model=data.get("model", model), usage=usage)


def call(model: str, system: str, tools: list[dict], messages: list) -> SimpleNamespace:
    body = {
        "model": model,
        "messages": _to_messages(system, messages),
        "tools": _to_tools(tools),
        "tool_choice": "required",  # same rule as Gemini mode ANY: never an uncited plain-text answer
        "max_tokens": config.llm_max_tokens,
    }
    if config.grok_reasoning_effort:
        # gpt-oss on Groq: "low" cuts hidden reasoning tokens ~3x, which matters under an 8k tokens/min free tier.
        body["reasoning_effort"] = config.grok_reasoning_effort
    try:
        r = httpx.post(f"{config.grok_base_url.rstrip('/')}/chat/completions", json=body, timeout=config.grok_timeout_s,
                       headers={"Authorization": f"Bearer {config.grok_api_key}"})
    except httpx.TimeoutException as e:
        raise ProviderTimeout(f"grok {type(e).__name__}") from e
    except httpx.TransportError as e:
        raise ProviderRetryable(f"grok {type(e).__name__}") from e
    if r.status_code != 200:
        raise from_status(provider_name(), r.status_code, r.text, r.headers)
    return _to_response(r.json(), model)
