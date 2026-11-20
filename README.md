# Grounded Card Service Agent

A credit-card service agent that only says what it can prove. Every sentence it sends must cite a policy section or account field it actually retrieved in that turn, and **code, not the model, checks that**. When the corpus doesn't cover a question, it says so and routes the customer instead of guessing.

**Stack:** Python 3.12 · FastAPI · ChromaDB (vectors, embedded) · PostgreSQL (tracking) · embeddings: Gemini → local `bge-small-en-v1.5` · LLM: Gemini → Groq/xAI → clear 503 error (no answer without a model).
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
        A["agent.py<br/>ReAct loop: reason → act → observe"] --> GI["guardrails/ (entry)<br/>sanitize · PII mask · flags · scope"]
        A --> GO["guardrails/ (exit)<br/>citation validator · output guard"]
        A --> SC["cache/semantic_cache.py"]
        A --> LLM["llm/llm_router.py<br/>Gemini → Groq/xAI → 503 LLM_LIMIT_REACHED"]
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
2. **Entry guardrail:** strips control characters, masks PII (card, Aadhaar, PAN, OTP…) and flags injection attempts or other customers' ids. A **scope check** then sorts the question into one of three tiers, using only the policy documents (their local bge-small vectors and their vocabulary):
   - **off-domain** ("write me a poem") gets a fixed reply, with no LLM call and no ticket;
   - **in domain but not covered** ("travel insurance") gets an honest gap and a human ticket;
   - **covered** questions are answered normally.

   Measured on 56 probes: 0 of 40 in-domain questions blocked, 14 of 16 off-domain caught (`guardrails/scope_guard.py`).
3. **Semantic cache:** an exact or paraphrase hit on an account-independent answer is re-checked for this customer (citations re-retrieved, output guard) and returned with **0 LLM calls**. The cache is shared by all customers, so **user-specific data never goes through it**. A personal question ("my", "I", a customer id, masked PII) skips the cache. An answer that used account data, a tool result or a service request is never stored ([docs/ARCHITECTURE.md §8](docs/ARCHITECTURE.md#8-semantic-cache-answering-from-the-database)).
4. **ReAct agent loop** (Reason → Act → Observe; at most 6 steps, 25 s): in each step the LLM states a one-sentence `thought` (a required argument of every tool, kept in the trace only), calls one or more tools, and reads every result as an observation. A `<loop_state>` tells it how many steps and seconds are left, and on the last step only `respond` is offered (forced finish). It acts only through tools ([docs/ARCHITECTURE.md §3.1](docs/ARCHITECTURE.md#31-the-react-agent-loop)):
   - `search_policy` runs hybrid retrieval. If the relevance gate says the question isn't covered, it returns `NO_RELEVANT_POLICY_FOUND`, which leads to a refusal and routing (a designed refusal).
   - `get_account_state`, `check_limit_increase_eligibility` and `raise_service_request` run through `tool_runner` (2 s timeout, retries, circuit breaker).
   - **ReAct loop:** a tool that still fails (Chroma down, retrieval over 10 s, a crash in a rule) never crashes the turn. The model receives a `TOOL_ERROR` observation and decides: retry once, reword the search, or answer honestly as `degraded`. Each tool may fail twice per turn, and `raise_service_request` is never retried ([docs/ARCHITECTURE.md §3.2](docs/ARCHITECTURE.md#32-when-a-tool-retrieval-or-the-database-fails)).
   - It finishes by calling `respond` with schema-valid claims, each carrying at least one citation.
5. **Exit guardrails:** the citation validator checks that every cited section, quote, number and reason code was retrieved **in this turn**, and the output guard blocks leaks and sales language. On failure the agent gets one repair attempt, then a safe response.
6. **What the customer sees when something fails:**

   | Failure | Response |
   |---|---|
   | Tool timeout or tool error | The model is told (ReAct observation) and retries once; otherwise a `degraded` reply |
   | All LLMs down or out of quota | **503 `LLM_LIMIT_REACHED`**: "We've reached our usage limit for today. Please try again tomorrow." The Idempotency-Key is freed, so the same request can be retried. Cache hits and off-domain replies need no model and still work. |
   | Unknown customer | 404 |
   | Anything uncaught | Safe error envelope; never a stack trace |
7. **Record:** one `req_res_log` row (provider, model, every failed attempt, tokens, cost, latency, spans, flags) and a JSON log line keyed by the request id.

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
| LLM chain: Gemini → Groq/xAI → 503 error | No single vendor outage takes the agent down. With no model left, a clear error beats an unreasoned answer. | Fallback models are less tested and produce more repairs; when every quota is used up, customers get an error until tomorrow. |
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

**Product scope: not needed for this corpus and brief.**
- **A reranker, or a search engine for BM25.** 16 chunks don't need them. Add a cross-encoder and OpenSearch or Postgres full-text search at 100× the corpus.
- **Real session auth.** An API key plus `customer_id` stand in for an authenticated session.
- **Token streaming.** Citations are validated on the complete answer, so SSE releases text only after validation.
- **Multilingual embeddings, an LLM judge.** EV-031 (Hinglish) is a declared expected failure.

**Production-rollout infrastructure: deliberately left out of a time-boxed prototype.** None of these changes what the agent says or how it's checked. They matter when the service runs for real customers at volume. The code leaves a seam for each: config-driven chains, one store module per concern, and a `requestId` that already joins logs, rows and responses. Full low-level design: [docs/ARCHITECTURE.md §10](docs/ARCHITECTURE.md#10-low-level-design).
- **Evaluation at scale:**
  - Re-run the eval several times on paid quotas. Today the free-tier limits, not reasoning, decide most failures.
  - Wire the eval gate into CI on paid quotas, with held-out and adversarial sets (multi-turn and indirect injection).
- **Multi-tenant scale:** per-tenant Chroma collections, Postgres row-level security, Redis for shared state, and per-tenant cost dashboards.
- **Security:** secrets in a vault, rotated (today: a local `.env`).
- **Resilience:**
  - A circuit breaker on Postgres, so a down database costs one fast failure instead of a 5 s wait per step. Measured live: a 504.
  - Async provider calls, with a per-request deadline passed to every network call.
- **Shared state:** Chroma in server mode, or pgvector / a managed vector DB. Embedded Chroma is a folder, fine for one process.
- **Observability:**
  - OpenTelemetry traces and metrics to Grafana or Datadog, and logs shipped to ELK or Loki instead of a local file.
  - Alerts on fallback rate, `LLM_LIMIT_REACHED` 503s, `tool_error:*`, `out_of_scope`, validation failures, p95 latency and cost per tenant.
- **Release safety:**
  - Canary or shadow rollout for new prompt and model versions.
  - Load tests (k6 / Locust) to size `LLM_CONCURRENCY`.
  - Weekly human review of sampled answers in the first weeks of rollout, to confirm correctness.
- **Packaging:**
  - A container image plus Kubernetes / Helm, or a managed platform, with separate dev, QA and prod environments.
  - Readiness probes on `/ready`.
  - Blue-green deploys.

## 8. What I would do next, given another day

- Add held-out paraphrases and recall@k, so the gate and prompt aren't fitted to these 32 cases.
- Detect policy conflicts automatically at ingest instead of listing them in `const/conflicts.py`.
- **Security and identity:**
  - Replace API key + body `customer_id` with OAuth2/OIDC, taking the customer from the token.
  - TLS everywhere, with a WAF in front.
  - Tighten PII handling with a data-residency review: embeddings and LLM calls currently leave the machine; consider Vertex AI in-region or self-hosted models.
- **Resilience:**
  - Paid / provisioned LLM and embedding quotas, plus a third LLM provider.
  - Graceful degradation drills (chaos tests).
- **Scale:**
  - Move to Redis: rate limits, circuit breakers, service-request dedupe, the embedding / retrieval caches, and the semantic cache (RediSearch vector index).
  - Several uvicorn workers behind a load balancer, with autoscaling.
- **Data:**
  - Versioned migrations (Alembic) instead of idempotent SQL files.
  - Partition `req_res_log` by month, with a retention policy and archival.
  - Backups plus point-in-time recovery.
  - Ingest as a CI/CD pipeline with document approval, so a policy change re-embeds and invalidates caches automatically.
- **Operations:** on-call runbooks.

## 9. What I would not trust about this submission, and how you'd find out it was wrong

| Don't trust | Why | How you'd find out |
|---|---|---|
| The relevance gate (bge ≥ 0.65, or ≥ 0.55 with keyword coverage ≥ ⅓) | Measured on 53 probes. 2/26 raw covered questions are refused until the agent reformulates them. 2/11 uncovered questions pass. The nearest uncovered question is only 0.004 below the bar. | Held-out probes; `GET /requests?flag=retrieval_miss` |
| Semantic-cache thresholds (Gemini 001: 0.86, Gemini 2: 0.89, bge: 0.90) | Measured on a handful of pairs. bge can't separate "forex markup charges?" (0.861) from "...on my Classic card?" (0.849), so it's set to miss rather than reuse. | Review answers flagged `cache_hit_semantic` |
| Number grounding | It's lexical: a correct number in the wrong sentence passes. | Human review of 20 random traces a week |
| Regex guardrails (PII, injection) | They match formats, not meaning. "four one one one…" isn't masked. | Red-team set; `pii_masked` / `injection_suspected` flag rates |
| My policy interpretations (D4, D5, working days → `sla_hours`) | They're judgement calls, not sign-offs. | Conduct and Model Risk review |
| Eval numbers | Free-tier noise of about ±3 cases. The prompt went v1 → v5 against these same cases (v6 adds only the ReAct step rules and has not had a full paid eval run yet), so it may be overfitted. | Repeated paid runs; held-out cases |
| Embeddings sent to Google | Each new question, PII-masked, is embedded through the Gemini API. | `embedding_fallback:*` flags; data-residency review |
