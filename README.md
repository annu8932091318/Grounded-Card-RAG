# Grounded Card Service Agent

A credit-card service agent that only says what it can prove. Every sentence it sends must cite a policy section or account field it actually retrieved in that turn, and **code, not the model, checks that**. When the corpus doesn't cover a question, it says so and routes the customer instead of guessing.

**Stack:** Python 3.12 · FastAPI · ChromaDB (vectors, embedded) · PostgreSQL (tracking) · embeddings: Gemini → local `bge-small-en-v1.5` · LLM: Gemini → Groq/xAI → no-LLM static mode.
**Demo video (5–10 min):** _link to be added_  
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

## 2. Architecture

```mermaid
flowchart LR
    C["Client<br/>Postman · curl"] -->|"x-api-key + Idempotency-Key"| MW
    subgraph API["FastAPI · src/app.py"]
        MW["middleware/<br/>auth · rate limit · idempotency · timeout"] --> R["routes/ → controller/"]
    end
    R --> A
    subgraph ORCH["Orchestration · modules/agent/"]
        A["agent.py<br/>tool-calling loop"] --> GI["guardrails/ (entry)<br/>sanitize · PII mask · flags"]
        A --> GO["guardrails/ (exit)<br/>citation validator · output guard"]
        A --> SC["cache/semantic_cache.py"]
        A --> LLM["llm/llm_router.py<br/>Gemini → Groq/xAI → static mode"]
    end
    subgraph LOGIC["Logic and retrieval"]
        RET["retrieval/<br/>BM25 + vector + RRF + gate"]
        DEC["decision/<br/>limit-increase rules (policy as code)"]
    end
    subgraph TOOLS["Tools · modules/tools/"]
        TR["tool_runner.py<br/>timeout · retry · circuit breaker"]
        T1["get_account_state"]
        T2["raise_service_request"]
    end
    A --> RET
    A --> DEC
    A --> TR
    TR --> T1
    TR --> T2
    RET --> CH[("ChromaDB<br/>one collection per embedding model<br/>+ quarantine")]
    T1 --> ACC[("data/accounts.json")]
    MW --> PG[("PostgreSQL<br/>req_res_log · ingest_log · semantic_cache")]
    SC --> PG
```

Logic, tools and orchestration are separate packages. `agent.py` is the only place that knows the order of the steps. Tools and rules never call the LLM, and the LLM never touches data except through a tool. More diagrams (ingest, fallback chain, guardrails, tables) are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 3. Execution flow: one `POST /chat`

1. **Middleware:** checks the API key and rate limit. It claims the `Idempotency-Key` (a replay returns the stored response) and starts a trace.
2. **Entry guardrail:** strips control characters, masks PII (card, Aadhaar, PAN, OTP…) and flags injection attempts or other customers' ids.
3. **Semantic cache:** an exact or paraphrase hit on an account-independent answer is re-checked for this customer (citations re-retrieved, output guard) and returned with **0 LLM calls**.
4. **Agent loop** (at most 6 iterations): the LLM gets the versioned system prompt plus the tool schemas, and must act only through tools:
   - `search_policy` runs hybrid retrieval. If the relevance gate says the question isn't covered, it returns `NO_RELEVANT_POLICY_FOUND`, which leads to a refusal and routing (a designed refusal).
   - `get_account_state`, `check_limit_increase_eligibility` and `raise_service_request` run through `tool_runner` (2 s timeout, retries, circuit breaker).
   - It finishes by calling `respond` with schema-valid claims, each carrying at least one citation.
5. **Exit guardrails:** the citation validator checks that every cited section, quote, number and reason code was retrieved **in this turn**, and the output guard blocks leaks and sales language. On failure the agent gets one repair attempt, then a safe response.
6. **What the customer sees when something fails:**

   | Failure | Response |
   |---|---|
   | Tool timeout | `degraded` reply plus a routed service request |
   | All LLMs down | Static mode (rules below) |
   | Unknown customer | 404 |
   | Anything uncaught | Safe error envelope; never a stack trace |
7. **Record:** one `req_res_log` row (provider, model, every failed attempt, tokens, cost, latency, spans, flags) and a JSON log line keyed by the request id.

**Static mode (every LLM down)** has its own strict exit rules, because no model is there to judge relevance:
- plain account lookups ("When is my payment due?") are answered from the customer's record, cited;
- otherwise only the policy **lines** covering the question's key terms are shown;
- flagged questions are refused, and action requests get "can't do that right now" plus a human handoff;
- never an unrelated policy section (`src/modules/agent/static_answers.py`).

## 4. Tool design and usage

| Tool (what the LLM sees) | What it does | Design choice |
|---|---|---|
| `search_policy(query)` | Hybrid search. Returns `<policy_excerpt>` blocks plus conflict notes. | Excerpts are wrapped as **data**, never instructions. Quarantined docs can't be returned. The model is told to search again with different wording. |
| `get_account_state()` | The supplied tool: the customer's record plus code-computed facts (fee waiver gap, months since last increase). | **No `customer_id` argument:** identity is bound server-side, so the model can't fetch another customer. The internal score is redacted. 400 ms latency and a 1-in-12 `UpstreamTimeout`, handled by retries and a breaker. |
| `check_limit_increase_eligibility()` | Deterministic rules from POL-CRD-001 (open dispute, 6-month bar; CRD-011's "3 months" overridden), CRD-003 (thin file), CON-004 (vulnerable) and KYC-008 (re-KYC). Returns `declined` or `referred` with approved reason codes. | **There is no approve tool.** The model can't grant an increase, whatever a document or the user says. That removes the injection risk at the capability level. |
| `raise_service_request(category, summary)` | The supplied tool: creates an SR in a POL-SVC-006 queue and returns `request_id`, `queue` and `sla_hours`. | The category is an **enum** of the real queues (`InvalidCategory` is never guessed). It's never blindly retried, and requests are de-duplicated for 10 minutes. |
| `respond(status, claims[], decision, routing)` | The final answer, as a JSON schema; `status` is one of `answered`, `answered_with_gap`, `declined`, `refused`, `request_raised`, `degraded`. | Structured output, so code can check every claim's citations before the customer sees anything. |

## 5. Key design decisions and architecture trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| Citations enforced by **code**, not the prompt | A prompt can't guarantee grounding; a validator can. | Lexical checks: a right number in the wrong sentence passes, and good answers sometimes need a repair round (latency). |
| Business rules as **code** (`decision/`) with no approve tool | Credit decisions must be deterministic and auditable. | Policy changes need a code change. A drift test guards the literals, not their meaning. |
| Hybrid BM25 + vectors + relevance gate | Exact terms (fees, codes) plus paraphrases. The gate makes refusal deliberate. | The gate threshold is tuned on a small probe set (§9). |
| Known conflicts resolved in config (`const/conflicts.py`) | D1 and D5 are known, and the prevailing doc is cited explicitly. | Doesn't scale: new conflicts need detection at ingest. |
| Embedding chain: Gemini → Gemini → local bge-small, **one Chroma collection per model** | Best ranking from Gemini; local bge never runs out of quota. Vectors of different models aren't comparable. | Ingest embeds everything 3 times, so 3× storage and embedding cost. |
| LLM chain: Gemini → Groq/xAI → static mode | No single vendor outage takes the agent down. | Fallback models are less tested and produce more repairs; static mode is honest but not a full answer. |
| **Embedded** Chroma + Postgres, no Docker | A plain local run, as asked. | One worker per Chroma folder. Several nodes need Chroma server mode plus Redis for shared state. |
| SSE **buffer-then-release** | Text is validated before the customer sees it. | No token-by-token streaming, so time to first token = full answer time. |
| Semantic cache only for account-independent, validated answers | Saves LLM calls without serving someone else's data. | Thresholds were measured on few pairs; a meaning-changing paraphrase could reuse an answer. |
| Free-tier models for development | Zero cost while building. | Rate limits, not reasoning, dominate eval noise. Prod costs are in [docs/COST_REPORT.md](docs/COST_REPORT.md). |

## 6. Measured results

- **Eval** (2026-11-14, 32 cases: 12 seeded + 20 own, 3 expected to fail): **17/32, and the gate failed.** 12 cases never reached a model because the free Gemini and Groq quotas ran out mid-run. Of the 20 that did, **16 passed**. The previous stack, with the same agent code, scored 28/32. Details: [eval/eval_report.md](eval/eval_report.md).
- **Demo traces** ([eval/demo_output.txt](eval/demo_output.txt)): injection, retrieval miss, tool timeout, contradictory policy (D1 → AA05) and primary model down, each with its span tree, tokens, latency and cost.
- **Cost:** about $0.003 per 3-turn conversation on Gemini 3.5 Flash-Lite with caching ([docs/COST_REPORT.md](docs/COST_REPORT.md)).
- **Unit tests:** 143 passing; no DB or API key needed.

## 7. Limitations: what I deliberately did not build, and why

- **Docker or a vector-DB server.** A plain run was the requirement: Chroma is a folder. Several workers would need Chroma server mode.
- **A reranker, or a search engine for BM25.** 16 chunks don't need them. Add a cross-encoder and OpenSearch or Postgres full-text search at 100× the corpus.
- **Real session auth.** An API key plus `customer_id` stand in for an authenticated session.
- **Token streaming.** Citations are validated on the complete answer, so SSE releases text only after validation.
- **Multilingual embeddings, an LLM judge, OpenTelemetry export, Redis.** These are premature on a single node. EV-031 (Hinglish) is a declared expected failure.

## 8. What I would do next, given another day

- Re-run the eval several times on paid quotas. Today the free-tier limits, not reasoning, decide most failures.
- Add held-out paraphrases and recall@k, so the gate and prompt aren't fitted to these 32 cases.
- Detect policy conflicts automatically at ingest instead of listing them in `const/conflicts.py`.
- Add multi-tenant scale: per-tenant Chroma collections, Postgres row-level security, Redis for shared state, and per-tenant cost dashboards.

## 9. What I would not trust about this submission, and how you'd find out it was wrong

| Don't trust | Why | How you'd find out |
|---|---|---|
| The relevance gate (bge ≥ 0.65, or ≥ 0.55 with keyword coverage ≥ ⅓) | Measured on 53 probes. 2/26 raw covered questions are refused until the agent reformulates them. 2/11 uncovered questions pass. The nearest uncovered question is only 0.004 below the bar. | Held-out probes; `GET /requests?flag=retrieval_miss` |
| Semantic-cache thresholds (Gemini 001: 0.86, Gemini 2: 0.89, bge: 0.90) | Measured on a handful of pairs. bge can't separate "forex markup charges?" (0.861) from "...on my Classic card?" (0.849), so it's set to miss rather than reuse. | Review answers flagged `cache_hit_semantic` |
| Number grounding | It's lexical: a correct number in the wrong sentence passes. | Human review of 20 random traces a week |
| Regex guardrails (PII, injection) | They match formats, not meaning. "four one one one…" isn't masked. | Red-team set; `pii_masked` / `injection_suspected` flag rates |
| My policy interpretations (D4, D5, working days → `sla_hours`) | They're judgement calls, not sign-offs. | Conduct and Model Risk review |
| Eval numbers | Free-tier noise of about ±3 cases. The prompt went v1 → v5 against these same cases, so it may be overfitted. | Repeated paid runs; held-out cases |
| Embeddings sent to Google | Each new question, PII-masked, is embedded through the Gemini API. | `embedding_fallback:*` flags; data-residency review |
