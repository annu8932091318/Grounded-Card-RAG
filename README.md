# Grounded Card Service Agent

A credit-card service agent that only says what it can prove. Every sentence it sends must cite a policy section or account field it actually retrieved in that turn, and **code, not the model, checks that**. When the corpus doesn't cover a question, it says so and routes the customer instead of guessing.

**Stack:** Python 3.12 · FastAPI · ChromaDB (vectors, embedded) · PostgreSQL (tracking) · embeddings: Gemini → local `bge-small-en-v1.5` · LLM: Gemini → Groq/xAI → no-LLM static mode.
**More:** [full run and debug guide](docs/RUNNING.md) · [architecture](docs/ARCHITECTURE.md) · [cost report](docs/COST_REPORT.md) · [Postman collection](postman/) · [eval report](eval/eval_report.md) · [demo traces](eval/demo_output.txt)

## 1. Install and run

The same steps work for local, QA and production. Only the values in `.env` change.

```bash
git clone <repo-url> Grounded-Card-RAG && cd Grounded-Card-RAG
python3.12 -m venv .venv                     # Windows: py -3.12 -m venv .venv
source .venv/bin/activate                    # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                         # Windows: copy .env.example .env   -> then edit it (below)
python -m scripts.migrate                    # creates the 3 tables (safe to re-run)
python -m scripts.ingest                     # policy corpus -> ChromaDB (skips unchanged docs)
python -m src.server                         # http://localhost:3000/api/v1 ; check: GET /api/v1/ready
```

**Database.** Put your Postgres connection in **one line** of `.env`. Any Postgres 14+ works: a local install, a Docker container, AWS RDS, Cloud SQL or Azure.
```ini
DATABASE_URL=postgresql://<user>:<password>@<host>:<port>/<database>
# local example: postgresql://agent:agent@localhost:5433/agent
# managed DB with TLS: postgresql://app:***@db.example.com:5432/agent?sslmode=require
```
- **The user and database already exist** (typical for QA or prod): run `python -m scripts.migrate`. It only needs rights to create tables in that database.
- **A fresh local Postgres:** run `python -m scripts.migrate --superuser postgres` once. It asks for the `postgres` password and creates the role and database named in `DATABASE_URL`. For a non-interactive run, set `PGSUPERPASSWORD`.

Installing Postgres itself (Windows service, pgAdmin, ports) is covered in [docs/RUNNING.md §2](docs/RUNNING.md#2-install-postgresql-once).

**Other settings in `.env`.** Everything not listed here has a working default, explained inline in `.env.example`.

| Setting | Local | QA / prod |
|---|---|---|
| `GEMINI_API_KEY` | AI Studio key (free tier is fine) | Paid-tier key, from your secret store |
| `GROK_API_KEY` + `GROK_BASE_URL` + `GROK_MODEL` | Groq or xAI key; optional | Required: it is the fallback when Gemini is down |
| `API_KEYS` / `ADMIN_API_KEY` | `demo-key-1` / `admin-key-1` | Long random values. Use `key:tenant` to bind a key to a tenant. |
| `ENV` | `development` (auto-reload) | `production` |
| `CHROMA_PATH` | `chroma_data` | A persistent disk path, e.g. `/var/lib/agent/chroma` |
| `LOG_DIR` / `LOG_LEVEL` / `LOG_TO_CONSOLE` | `logs` / `info` / `1` | Your log volume / `info` / `0` |
| `TOOL_FAIL_RATE`, `LLM_FORCE_PRIMARY_FAIL` | Demo switches (timeout, outage) | `0` |

**Rules for QA and prod:**
- Never commit `.env`; inject these values as environment variables.
- Run **one worker per Chroma folder**. Embedded Chroma and the in-process rate limiter and caches aren't shared between processes. Scaling out means Chroma server mode plus Redis ([docs/RUNNING.md §12](docs/RUNNING.md#12-semantic-cache-answers-from-the-database-not-the-llm)).
- The local embedding model (~67 MB) downloads once on the first ingest. Pre-download it on hosts with no internet access.

**Try it:**
```bash
curl -X POST http://localhost:3000/api/v1/chat -H "x-api-key: demo-key-1" -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-0001" -d '{"customer_id":"CUST-2003","message":"I want my credit limit increased to 3 lakh."}'
```
- Every POST needs an `Idempotency-Key` header. Sending the same key again replays the stored response.
- **Postman:** import `postman/Grounded-Card-Agent.postman_collection.json`. It has 59 requests in 12 folders, each with a saved sample response, and works on import against `localhost:3000`.
- **Checks:** `pytest -q` (unit tests, no DB or key needed), `python -m eval.run_eval` (32 cases, exits 1 if the gate fails), `python -m scripts.demo --pause 60` (traced demo requests).

## 2. What I built

**Ingest.** Policies are chunked by section and embedded with each model in the chain: `gemini-embedding-001`, then `gemini-embedding-2`, then local `bge-small-en-v1.5`. Each model gets its own ChromaDB collection. POL-OPS-012, which carries hidden instructions, is quarantined at ingest into a collection that search never opens.

**Retrieval.** Hybrid BM25 plus vectors, fused with RRF. A relevance gate decides whether the question is covered at all. It runs on local bge-small, so it costs nothing and can't run out of quota. Known policy conflicts (D1: 3 vs 6 months; D5: thin file) attach the prevailing document.

**Agent.** The model can only act through tools: `search_policy`, `get_account_state` (identity bound server-side, internal score redacted), `check_limit_increase_eligibility` (deterministic policy-as-code; there is **no approve tool**), `raise_service_request` and `respond`. Tool timeouts are retried and covered by a circuit breaker. If the account tool fails, the customer gets an honest `degraded` reply plus a routed request, never a guess.

**Guardrails.**
- **On the way in:** API key, rate limit, mandatory Idempotency-Key, schema validation and PII masking.
- **On the way out:** a citation validator (every claim's source, quote, number and reason code must have been retrieved this turn) and an output guard (no score leaks, approval language, sales to vulnerable customers, prompt leaks, other customers' data or PII). The agent gets one repair attempt; after that it returns a safe response.

**Deploy and scale.**
- **Tracing:** spans, tokens, cost and latency for every call go to Postgres (`req_res_log`, `/requests/summary`, `/metrics`).
- **Prompt versioning:** prompts are versioned files (`prompts/system.vN.md`). CI fails if one is edited in place.
- **Eval gate in CI:** the build fails on any critical failure, on more than one unexpected failure, or below an 85% pass rate.
- **Fallbacks:** across models and across providers, then static mode. Static mode has its own strict exit rules, because no model is there to judge relevance:
  - plain account lookups ("When is my payment due?") are answered from the customer's record, cited;
  - otherwise only the policy **lines** covering the question's key terms are shown;
  - flagged questions are refused, and action requests get "can't do that right now" plus a human handoff;
  - never an unrelated policy section (`src/modules/agent/static_answers.py`).
- **Semantic answer cache:** a repeated question costs no LLM call.
- **Streaming:** SSE, buffer-then-release.
- **Cost per conversation:** see [the cost report](docs/COST_REPORT.md).

**Measured.** The latest eval (2026-11-14) scored **17/32, and the gate failed**. 12 cases never reached a model because the free Gemini and Groq quotas ran out mid-run. Of the 20 that did, **16 passed**. The previous stack, with the same agent code, scored 28/32. [Demo traces](eval/demo_output.txt) show each Break scenario: injection, retrieval miss, tool timeout, contradiction, and primary model down.

## 3. What I deliberately did not build, and why

- **Docker or a vector-DB server.** A plain run was the requirement: Chroma is a folder. Several workers would need Chroma server mode.
- **A reranker, or a search engine for BM25.** 16 chunks don't need them. Add a cross-encoder and OpenSearch or Postgres full-text search at 100× the corpus.
- **Real session auth.** An API key plus `customer_id` stand in for an authenticated session.
- **Token streaming.** Citations are validated on the complete answer, so SSE releases text only after validation.
- **Multilingual embeddings, an LLM judge, OpenTelemetry export, Redis.** These are premature on a single node. EV-031 (Hinglish) is a declared expected failure.

## 4. What I would do next, given another day

- Re-run the eval several times on paid quotas. Today the free-tier limits, not reasoning, decide most failures.
- Add held-out paraphrases and recall@k, so the gate and prompt aren't fitted to these 32 cases.
- Detect policy conflicts automatically at ingest instead of listing them in `const/conflicts.py`.
- Add multi-tenant scale: per-tenant Chroma collections, Postgres row-level security, Redis for shared state, and per-tenant cost dashboards.

## 5. What I would not trust about this submission, and how you'd find out it was wrong

| Don't trust | Why | How you'd find out |
|---|---|---|
| The relevance gate (bge ≥ 0.65, or ≥ 0.55 with keyword coverage ≥ ⅓) | Measured on 53 probes. 2/26 raw covered questions are refused until the agent reformulates them. 2/11 uncovered questions pass. The nearest uncovered question is only 0.004 below the bar. | Held-out probes; `GET /requests?flag=retrieval_miss` |
| Semantic-cache thresholds (Gemini 001: 0.86, Gemini 2: 0.89, bge: 0.90) | Measured on a handful of pairs. bge can't separate "forex markup charges?" (0.861) from "...on my Classic card?" (0.849), so it's set to miss rather than reuse. | Review answers flagged `cache_hit_semantic` |
| Number grounding | It's lexical: a correct number in the wrong sentence passes. | Human review of 20 random traces a week |
| Regex guardrails (PII, injection) | They match formats, not meaning. "four one one one…" isn't masked. | Red-team set; `pii_masked` / `injection_suspected` flag rates |
| My policy interpretations (D4, D5, working days → `sla_hours`) | They're judgement calls, not sign-offs. | Conduct and Model Risk review |
| Eval numbers | Free-tier noise of about ±3 cases. The prompt went v1 → v5 against these same cases, so it may be overfitted. | Repeated paid runs; held-out cases |
| Embeddings sent to Google | Each new question, PII-masked, is embedded through the Gemini API. | `embedding_fallback:*` flags; data-residency review |
