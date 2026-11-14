# Cost report: what this agent costs in production

**Date:** 2026-11-15. **Prices:** USD per million tokens, standard (non-batch) paid tier. They are in [`src/modules/llm/pricing.py`](../src/modules/llm/pricing.py), which also drives the live `cost_usd` in `req_res_log` and `/metrics`. Claude prices come from Anthropic's model table (cached 2026-11-03). Gemini, OpenAI and Groq prices come from third-party pricing trackers dated October–November 2026 that cite the providers' pages. **Re-check every price on the provider's own pricing page before quoting one to a customer.**

## 1. The token profile (measured, not assumed)

These come from `req_res_log`, 26 `/chat` turns on 2026-11-14 that reached a model:

| | Mean | p50 | p95 |
|---|---|---|---|
| Input tokens per turn (summed over the agent loop's LLM calls) | 3,750 | 3,189 | 7,227 |
| Output tokens per turn | 217 | 198 | 385 |

- **The fixed prefix is large.** The system prompt (v5, ~2,000 tokens) and the tool schemas (~640 tokens) are resent on every LLM call. They make up about 65% of input tokens, and they're what prompt caching discounts.
- **Embeddings cost close to nothing.** A question is ~20 tokens, so the cost is negligible at any listed embedding price. The local fallback (`bge-small-en-v1.5`) costs $0.
- **Semantic-cache hits cost $0 LLM.** A repeated, account-independent question is answered from Postgres with no model call.

## 2. Cost per turn and per conversation

Assumptions (change them and re-run the arithmetic in this section):
- A conversation is **3 turns**.
- **Cached** means 65% of input is billed at the cache-read price.
- The cost of writing the cache is ignored, because it's paid once per 5-minute window, not per turn.

| Model | $ in / out per MTok | Per turn, no cache (mean) | Per turn, no cache (p95) | Per turn, cached | **Per conversation, cached** | Per 1M conversations / month | …with a 30% semantic-cache hit rate |
|---|---|---|---|---|---|---|---|
| gpt-5-nano | 0.05 / 0.40 | $0.00027 | $0.00052 | $0.00017 | **$0.0005** | $494 | $346 |
| gemini-2.5-flash-lite | 0.10 / 0.40 | $0.00046 | $0.00088 | $0.00024 | **$0.0007** | $727 | $509 |
| gpt-oss-20b (Groq) | 0.075 / 0.30 | $0.00035 | $0.00066 | $0.00026 | **$0.0008** | $765 | $535 |
| claude-haiku-5-5 | 0.10 / 0.50 | $0.00048 | $0.00092 | $0.00026 | **$0.0008** | $792 | $555 |
| grok-4-fast-non-reasoning | 0.20 / 0.50 | $0.00086 | $0.00164 | $0.00049 | **$0.0015** | $1,479 | $1,035 |
| gpt-oss-120b (Groq) | 0.15 / 0.60 | $0.00069 | $0.00132 | $0.00051 | **$0.0015** | $1,530 | $1,071 |
| gemini-3.1-flash-lite | 0.25 / 1.50 | $0.00126 | $0.00238 | $0.00072 | **$0.0021** | $2,144 | $1,501 |
| gpt-5-mini | 0.25 / 2.00 | $0.00137 | $0.00258 | $0.00082 | **$0.0025** | $2,469 | $1,728 |
| gemini-3.5-flash-lite (current primary) | 0.30 / 2.50 | $0.00167 | $0.00313 | $0.00101 | **$0.0030** | $3,028 | $2,120 |
| gemini-2.5-flash | 0.30 / 2.50 | $0.00167 | $0.00313 | $0.00101 | **$0.0030** | $3,028 | $2,120 |
| gemini-3-flash-preview | 0.50 / 3.00 | $0.00253 | $0.00477 | $0.00143 | **$0.0043** | $4,287 | $3,001 |
| claude-sonnet-5-5 | 2.00 / 10.00 | $0.00967 | $0.01830 | $0.00504 | **$0.0151** | $15,116 | $10,581 |
| claude-opus-5-5 | 4.00 / 20.00 | $0.01934 | $0.03661 | $0.01008 | **$0.0302** | $30,232 | $21,163 |

## 3. What the numbers mean

1. **At this volume, the model bill is small next to the cost of one wrong answer.** Even Claude Opus 5.5 is about 3 cents per conversation. A single mis-sold limit increase or compliance breach costs more than a month of any row above. So choose the model by **eval pass rate first** (`python -m eval.run_eval`, the CI gate), then take the cheapest model that passes. Don't start from price.
2. **Caching is the biggest free saving: about 40–50% per turn.** It discounts the fixed 2.6k-token prefix. To keep it working:
   - **Gemini 2.5+ and OpenAI** cache implicitly, but only if the prefix is byte-identical between calls. Never put a timestamp or request id in the system prompt; `AS_OF_DATE` is pinned for this reason.
   - **Claude** needs an explicit `cache_control` breakpoint after the tool schemas and system prompt. Its minimum cacheable prefix depends on the model, so check that ~2.6k tokens qualifies on the model you pick.
3. **The semantic cache saves money on top of that.** A 30% hit rate on account-independent questions (fees, rates, routing) cuts another 30% of LLM calls, plus their latency.
4. **Reasoning models cost more than these rows show.** The output figures were measured on Gemini Flash-Lite and gpt-oss at low reasoning effort. Claude 5.5 models (thinking always on; control it with `effort`) and the GPT-5 family bill their reasoning tokens as output. At `low` effort, expect output to be 1.5–3× the measured 217 tokens, so re-measure with the eval before you budget.
5. **Batch APIs (50% off) don't fit here.** A customer is waiting. They'd only help offline eval runs.
6. **p95 turns cost about 1.9× the mean.** That's multi-step limit-increase questions: a policy search, then the account tool, then the eligibility check. Budget on p95.

## 4. Recommendation for production

- **Primary:** the cheapest model that passes the full eval gate (≥ 85% pass rate, 0 critical failures) on paid quotas. On today's evidence that means trying `gemini-3.5-flash-lite` or `gemini-2.5-flash` first (about $3k per 1M conversations), then stepping up to `claude-sonnet-5-5` (about $15k) if the critical cases (EV-011, EV-026, EV-027, EV-030) still fail.
- **Fallback:** a different provider at similar quality, so one vendor outage doesn't take the agent down (`gpt-oss-120b` on Groq or `gpt-5-mini`). Static mode stays as the last resort.
- **Measure live:** `GET /api/v1/requests/summary?days=7` gives cost per source and per model, and `SELECT avg(cost_usd) FROM req_res_log WHERE path='/api/v1/chat'` gives cost per turn. Alert when cost per conversation drifts above 2× this table.

