# Grounded Card Service Agent

Python port of the build spec (`README.pdf`), keeping its layered layout (`routes/ controller/ middleware/ modules/ helper/ const/ db/`). Stack: FastAPI · Postgres 16 + pgvector · local MiniLM embeddings (fastembed/ONNX) · Anthropic Claude (Sonnet 5.5 primary, Haiku 5.5 fallback) · pydantic · pytest.

## 1. What I built
A service agent for credit-card questions. The policy corpus is chunked by section into Postgres. Hybrid retrieval fuses a full-text leg (`tsvector`) and a pgvector leg with Reciprocal Rank Fusion, behind a relevance gate. POL-OPS-012 is quarantined at ingest: it is stored for audit but can never be retrieved or cited. Claude can call five tools: `search_policy`, `get_account_state`, `check_limit_increase_eligibility` (deterministic policy-as-code), `raise_service_request` and `respond`. It must finish with `respond`. `customer_id` is bound server-side, and **no tool can approve a limit**.

A code-level validator (`modules/guardrails/citation_validator.py`) checks every claim against what was retrieved or returned *in this turn*:
- the cited doc and section exist;
- quotes are verbatim;
- every number appears in a cited source;
- reason codes are AA01–AA06 *and* supported by the rules;
- the status is consistent with the claims.

A failed answer gets one repair attempt, then a safe refusal routed to GENERAL. The customer message is rendered only from validated claims. An output guard blocks score leaks, approval language, sales content to vulnerable customers, prompt leaks and other customers' ids.

The rest of the build:
- **Failure paths:** tool timeout handling (retry with backoff, circuit breaker, honest `degraded` status); service-request idempotency; model fallback, then a no-LLM static mode.
- **Observability:** traces and spans with token, cost and latency figures in Postgres; `/metrics`.
- **Versioning and gates:** prompts are versioned by sha; a CI eval gate covers 12 seed + 20 own cases (3 expected to fail).

**Pass rate: not yet measured.** This environment had no API key or running Postgres. Run `python -m eval.run_eval` and paste the four numbers from `eval/results/*.md` here: overall, seed, own, and expected-fail hit / unexpected pass. Unit and loop tests: 75 passing (`pytest`).

## 2. What I deliberately did not build
- **Redis.** Single node, so in-process LRU is enough. Cache keys already include the tenant.
- **A reranker.** Unnecessary at 17 chunks; I'd add a cross-encoder at 100× corpus.
- **A second LLM provider.** It slots into `llm_router._chain()`.
- **Real session auth.** An API key plus the body `customer_id` stand in for it.
- **True BM25.** Postgres full-text search stands in.
- **Multilingual embeddings.** EV-031 (Hinglish) is an expected failure.
- **Incremental claim streaming.** SSE is buffer-then-release because citations need the whole answer.
- **Also omitted:** an LLM judge, OpenTelemetry export, and a UI.

## 3. What I'd do next (another day)
- Run the eval on the real model and tune the gate threshold and prompt (v2 adds a date-gap rule for EV-029).
- Detect conflicts automatically at ingest.
- Add multilingual embeddings with Hinglish cases.
- Add recall@k metrics on labelled chunks.
- Build an adversarial eval set (indirect injection, multi-turn).
- Add Postgres row-level security per tenant and partition chunks by tenant.
- Move shared state (rate limits, breakers, caches) to Redis.
- Load-test concurrency (`LLM_CONCURRENCY`) and build a per-tenant cost dashboard.

## 4. What I would not trust, and how you'd find out
- **(a) The relevance gate.** I measured that MiniLM scores the in-corpus query "foreign currency markup" at only 0.24 against the long fee-table chunk, so a vector-only gate at 0.30 would refuse it. The gate is now *vector ≥ 0.30 OR a keyword hit on a non-generic term*. That threshold was tuned on about 25 probe queries. Partly-covered topics ("annual fee amount", 0.53) pass the gate, so the LLM's own gap detection carries D3. Check with held-out paraphrases and out-of-corpus questions, and with `/traces?flag=retrieval_miss`.
- **(b) Number grounding is lexical.** A correct number used in the wrong sentence still passes. Human-review 20 random traces.
- **(c) Hardcoded thresholds in `decision/`.** `test_policy_drift.py` guards the literals, not their meaning. Diff on every policy version.
- **(d) Interpretations that need sign-off.** These are mine: the D4 wording (no approved code for vulnerable or low-utilisation declines), the D5 rule that thin-file customers skip the §1 criteria, and working days converted to `sla_hours` at 24 hours per day. They need Conduct and Model Risk sign-off.
- **(e) Injection defence is layered but unproven** against indirect injection in tool output.
- **(f) The Anthropic integration is only exercised with a scripted fake model** in tests (`tests/test_agent_loop.py`). The strict tool schemas and the server-side fallback beta are unverified against the live API, and both are config flags. Check `/traces?flag=citation_validation_failed`, the eval report and CI.

## Run
```bash
docker compose up -d db
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # (bin/ on Linux/macOS)
cp .env.example .env                       # add ANTHROPIC_API_KEY
python -m scripts.migrate && python -m scripts.ingest
python -m src.server                       # API on :3000, base path /api/v1
pytest -q                                  # unit + agent-loop tests (no DB / key needed)
python -m eval.run_eval                    # eval/results/*.md, exit 1 if the gate fails
python -m scripts.demo                     # 6 demo requests + span trees (paste output into the submission)
curl -s localhost:3000/api/v1/chat -H 'x-api-key: demo-key-1' -H 'content-type: application/json' \
  -d '{"customer_id":"CUST-2003","message":"I want my credit limit increased to 3 lakh."}'
```
Or start everything with `docker compose up --build`, which runs migrate, ingest and serve. `LLM_FORCE_PRIMARY_FAIL=1` demonstrates the fallback model and `TOOL_FAIL_RATE=1` the timeout path.
