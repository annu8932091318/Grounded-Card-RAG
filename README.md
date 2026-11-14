# Grounded Card Service Agent

A credit-card service agent that only says what it can prove. Every sentence it sends must cite a policy section or account field it actually retrieved in that turn, and **code, not the model, checks it**.

**Stack:** Python 3.12 · FastAPI · ChromaDB (vectors) · PostgreSQL (tracking records) · local MiniLM embeddings · Google Gemini → Groq/xAI Grok → no-LLM static mode. Runs locally or in the VS Code debugger. No Docker.

Diagrams: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · API: [postman/](postman/) · Setup: [§5](#5-run-it-locally).

---

## 1. What I built

A FastAPI service that answers credit-card customers from a governed policy corpus and their own account record.

**Ingest.** Policies are chunked by section, embedded locally (all-MiniLM-L6-v2, 384-d) and stored in **ChromaDB**. A document carrying hidden instructions (POL-OPS-012) is quarantined at ingest into a separate collection that search never opens.

**Retrieval.** Hybrid: an in-memory **BM25** keyword leg and a Chroma **vector** leg, fused with Reciprocal Rank Fusion and guarded by a relevance gate. Known policy conflicts attach the prevailing document.

**Agent loop.** The LLM (**Google Gemini** first, **Groq/xAI Grok** as fallback, **static mode** if all fail) must answer through tools:
- `search_policy`;
- `get_account_state` (identity bound server-side, internal score redacted);
- `check_limit_increase_eligibility` (deterministic policy-as-code; there is **no approve tool**);
- `raise_service_request`;
- `respond`.

**Exit guardrails.** A **citation validator** rejects any claim whose source, quote, number or reason code wasn't retrieved this turn. An **output guard** blocks score leaks, approval language, sales to vulnerable customers, prompt leaks, other customers' ids and PII. One repair attempt, then a safe response; it never guesses.

**Entry guardrails.** API key, rate limit, mandatory **Idempotency-Key** with replay, schema validation, control-character stripping and **PII masking**.

**Postgres: three tables.**
- **`req_res_log`** has one row per API call, keyed by its Idempotency-Key: source, response time, the provider and model that answered, every failed model attempt, tokens and cost (credits), the masked request and the response.
- **`ingest_log`** has one row per ingested document.
- **`semantic_cache`** holds answers reused without the LLM: an exact or paraphrase match, re-checked for each customer.

Every event is also written to `logs/<dd-mm-yyyy>output.log` with file, function and request id.

**Quality.** 129 unit tests (no DB or key needed). A 32-case eval with deterministic judges and a CI gate.

**Measured eval** (2026-11-14, prompt v5, real LLMs, this stack). Read the conditions:
- **Run 1: 17/32 overall, gate failed.** 20 cases reached a model (15 Gemini, 4 Groq, 1 n/a); **16 of those 20 passed**. The other 12 got no model: Gemini's free daily quota ran out mid-run (HTTP 429), the Groq fallback hit its 8k-tokens-per-minute limit, and static mode answered honestly.
- Of the 4 failures with a model:
  - **EV-004 and EV-011** were a judge false positive. "The approved reason code…" counted as approval language. The judge now uses the output guard's existing, tested exclusion.
  - **EV-005 and EV-023** were status judgement calls (`answered_with_gap` vs `answered`).
- **Run 2 is invalid**, not a score: both providers' free quotas were exhausted for the day (4/32, 28 cases with no model).
- The previous stack (same agent code; pgvector instead of Chroma, Claude unfunded so Gemini answered) reached **28/32** on 2026-11-14. That history is in `eval/results/`.
- Reports: `eval/eval_report.md` (run 1). A trustworthy number needs a re-run when the quotas reset, or on paid tiers.

Verified live today, case by case (§5.6, Postman), with real answers from both providers:
- **Gemini:** fee waiver, injection refusal, PII masking.
- **Groq**, with Google forced down: AA04 decline, 3.5% markup.
- The stream, all error codes, idempotent replay, semantic-cache hits (0.4 s, no LLM), and `req_res_log` / `ingest_log` / `semantic_cache` rows on Postgres 18 (port 5433).

## 2. What I deliberately did not build, and why

- **Docker / a vector-DB server.** The requirement was a plain local run. Chroma runs embedded (a folder); Postgres is a normal local install. Several API workers would need Chroma server mode (`chroma run` + `HttpClient`).
- **A reranker.** 16 chunks don't need one; add a cross-encoder at 100× the corpus.
- **A search engine for BM25.** The in-memory index rebuilds in milliseconds at this size. It moves to OpenSearch/Postgres FTS at thousands of documents.
- **Real session auth.** An API key plus `customer_id` in the body stand in for an authenticated session.
- **Multilingual embeddings.** Hinglish (EV-031) is a declared expected failure.
- **Token streaming.** SSE is buffer-then-release, because citations are validated on the complete answer.
- **An LLM judge, OpenTelemetry export, a UI, Redis.** Single node: in-process rate limiter, breakers and caches are enough here.

## 3. What I would do next, given another day

- Re-run the eval on paid quotas several times. The free-tier rate limits, not reasoning, decide most failures today.
- Add held-out paraphrases and recall@k on labelled chunks, to check the relevance gate and the prompt aren't fitted to these 32 cases.
- Detect conflicts automatically at ingest instead of listing them in `const/conflicts.py`.
- Add multilingual embeddings plus Hinglish cases.
- Build an adversarial set: indirect injection in tool output, multi-turn.
- Move the semantic cache and shared state (rate limits, breakers, caches) to Redis (§5.11); run Chroma in server mode; export spans via OpenTelemetry.
- Add Postgres row-level security per tenant and a per-tenant cost dashboard from `req_res_log`.

## 4. What I would not trust about this submission, and how you'd find out it was wrong

| Don't trust | Why | How you'd find out |
|---|---|---|
| The relevance gate (vector ≥ 0.30 **or** a specific keyword hit) | Tuned on ~25 probe queries. MiniLM scores the in-corpus "foreign currency markup" at only 0.24. | Held-out paraphrases + out-of-corpus questions; watch `GET /requests?flag=retrieval_miss`. |
| Number grounding | Lexical: a correct number in the wrong sentence passes. | Human review of 20 random traces a week. |
| Thresholds hard-coded in `decision/` | `test_policy_drift.py` guards the literals, not their meaning. | Diff every policy version; the drift test fails on literal changes. |
| My interpretations | D4 wording (no approved code for vulnerable / low-utilisation declines), D5 (thin-file skips the §1 criteria), working days → `sla_hours` at 24 h/day. | Conduct and Model Risk sign-off. |
| Regex guardrails (PII, injection) | They match formats, not meaning ("four one one one…" isn't masked). | Red-team set; `pii_masked` / `injection_suspected` flag rates in traces. |
| Static mode | It returns the single best chunk for the raw question: honest and routed, but not an answer. | `GET /requests?flag=static_mode`. |
| Semantic cache threshold (0.87) | Measured on ~10 question pairs (paraphrase 0.88–0.90, different detail 0.83). A paraphrase that changes meaning above 0.87 could reuse an answer; numbers, re-retrieval and the output guard narrow but don't close that gap. | `GET /requests?flag=cache_hit_semantic`: review those answers; raise the threshold if any is wrong. |
| Eval numbers | Free-tier quotas (Gemini daily, Groq 8k tokens/min) add ±3 cases of noise. The prompt went v1 → v5 against these same 32 cases (overfitting risk). I aligned one judge check with the output guard's existing, tested exclusion: "the approved reason code AA05" is not approval language. | Run each case several times on paid quotas; validate on held-out cases; check `citation_validation_failed`, `quote_dropped`, `agent_timeout`. |
| Fallback provider quality | Groq's gpt-oss models were exercised live but less than Gemini, and sometimes emit malformed tool calls (now retried once). | `GET /requests/summary` → `llm_attempts_by_model` and `answer_quality_by_model`. |

---

## 5. Run it locally

### 5.1 Prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | 3.11 or 3.12 (tested on 3.12.10) | `python --version` |
| PostgreSQL | 14+ (tested on 16) | `psql --version` |
| Git | any | `git --version` |
| VS Code (optional) | + extensions **Python**, **Python Debugger**, *Markdown Preview Mermaid Support* | |
| Postman (optional) | any recent | |
| API keys | Google AI Studio key (primary) + Groq or xAI key (fallback). Either alone works; with none you get static mode. | |

### 5.2 Install PostgreSQL (once)

**Windows.** Either:
- run the EDB installer from <https://www.postgresql.org/download/windows/> (it includes pgAdmin), or
- `winget install PostgreSQL.PostgreSQL.18`.

Remember the `postgres` password and the port. This project's setup uses **5433** (5432 is the default); the port must match `DATABASE_URL` (§5.4).

Check it's running:
```powershell
& "C:\Program Files\PostgreSQL\18\bin\pg_isready.exe" -h localhost -p 5433
```

*No response?* The Windows service may not have been registered (seen after a repeated install). Fix it from **PowerShell run as Administrator**:
```powershell
$pg = "C:\Program Files\PostgreSQL\18"
icacls "$pg\data" /grant "NT AUTHORITY\NetworkService:(OI)(CI)F" /T
& "$pg\bin\pg_ctl.exe" register -N postgresql-x64-18 -U "NT AUTHORITY\NetworkService" -D "$pg\data" -S auto
Start-Service postgresql-x64-18          # starts now and on every boot
```
Without admin rights you can start it for the current session with `& "$pg\bin\pg_ctl.exe" -D "$pg\data" -l "$env:TEMP\pg.log" start`.

**pgAdmin:** *Register → Server…* → Name `local-18`; on the *Connection* tab, Host `localhost`, Port `5433`, Username `postgres` (or `agent` / password `agent`). Tables appear under *Databases → agent → Schemas → public → Tables* after §5.5. The server's port lives in `C:\Program Files\PostgreSQL\18\data\postgresql.conf` (`port = 5433`), not in pgAdmin.

**macOS:** `brew install postgresql@16 && brew services start postgresql@16`. **Ubuntu/Debian:** `sudo apt install postgresql`.

You don't need to create the role or database by hand: `python -m scripts.migrate --superuser postgres` (§5.5) does both.

> No admin rights? The `pgserver` pip package ships portable Postgres binaries (`initdb`, `pg_ctl`). That's how this README's steps were verified on a machine without a Postgres install.

### 5.3 Get the code and install Python packages

```powershell
git clone <repo-url> Grounded-Card-RAG
cd Grounded-Card-RAG
py -3.12 -m venv .venv                 # macOS/Linux: python3.12 -m venv .venv
.venv\Scripts\Activate.ps1             # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -r tools/chroma_peek/requirements.txt   # optional: the vector-store viewer (streamlit, pandas)
```

PowerShell blocks `Activate.ps1`? Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
Embeddings use **no API**: `fastembed` runs `sentence-transformers/all-MiniLM-L6-v2` locally on CPU with ONNX Runtime (384-d). The ~90 MB model downloads once from Hugging Face (`Qdrant/all-MiniLM-L6-v2-onnx`) on the first ingest, is cached in `%TEMP%astembed_cache`, and works offline after that.

### 5.4 Configure `.env`

```powershell
copy .env.example .env                 # macOS/Linux: cp .env.example .env
```

Set at least:

```ini
DATABASE_URL=postgresql://agent:agent@localhost:5433/agent
GEMINI_API_KEY=...                     # https://aistudio.google.com/apikey
GEMINI_MODEL=gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash   # each model = one fallback step (own free quota)
GROK_API_KEY=...                       # Groq (gsk_...) or xAI (xai-...)
GROK_BASE_URL=https://api.groq.com/openai/v1             # xAI: https://api.x.ai/v1
GROK_MODEL=openai/gpt-oss-120b,openai/gpt-oss-20b        # xAI: grok-4-fast-non-reasoning
GROK_REASONING_EFFORT=low                                # Groq gpt-oss only; empty for xAI
```

Every other setting is documented inline in `.env.example`. `.env` is git-ignored: never commit keys.

### 5.5 Create tables and load the corpus

```powershell
python -m scripts.migrate --superuser postgres   # FIRST TIME: asks for the postgres password once, creates role
                                                 # "agent" + database "agent" (from DATABASE_URL), then the tables
python -m scripts.migrate                        # afterwards: tables only (idempotent, safe to re-run)
python -m scripts.ingest                         # 12 docs -> 16 searchable chunks + 1 quarantined, into ./chroma_data
```

`migrate` leaves exactly three tables, `req_res_log`, `ingest_log` and `semantic_cache` (§5.10), dropping tables from older versions. The superuser password is never stored; for a non-interactive run, set `PGSUPERPASSWORD` in the environment for that one command.

Re-running ingest skips unchanged documents (content hash). Use `--force` to re-embed everything.

### 5.6 Run the API

```powershell
python -m src.server            # http://localhost:3000/api/v1  (auto-reload when ENV=development)
```

Check it:

```powershell
curl http://localhost:3000/api/v1/ready
curl -X POST http://localhost:3000/api/v1/chat `
  -H "x-api-key: demo-key-1" -H "Content-Type: application/json" `
  -H "Idempotency-Key: demo-0001" `
  -d '{\"customer_id\":\"CUST-2003\",\"message\":\"I want my credit limit increased to 3 lakh.\"}'
```

macOS/Linux: use `\` for line continuation and plain `"` inside single quotes.
**Every POST needs `Idempotency-Key`** (8–128 chars of letters, digits, `. _ : -`). Without it you get `400 IDEMPOTENCY_KEY_REQUIRED`. Re-sending the same key and body returns the stored response (`Idempotent-Replayed: true`). The key is also the call's `requestId`: in the logs, as `req_res_log.idempotency_key`, and in the `x-request-id` header.

Interactive API docs: <http://localhost:3000/docs>.

### 5.7 Debug in VS Code

1. Open the folder, then `Ctrl+Shift+P` → **Python: Select Interpreter** → `.venv`.
2. **Run and Debug** (`Ctrl+Shift+D`), pick a configuration from [.vscode/launch.json](.vscode/launch.json), press **F5**:

| Configuration | What it does |
|---|---|
| **App: API server (debug)** | uvicorn on :3000 with breakpoints in `src/**` |
| **Migrate: Postgres tables** | `scripts.migrate` (first time, run `--superuser postgres` in a terminal, §5.5) |
| **Ingest: policy corpus → ChromaDB** | `scripts.ingest` |
| **Chroma Peek: vector store viewer** | Streamlit viewer on :8501 |

To try the fallback or static mode in the debugger, set `LLM_FORCE_PRIMARY_FAIL=1` or `LLM_DISABLED=1` in `.env` and restart.

Good first breakpoints: `src/modules/agent/agent.py` → `run_agent`, `_loop`; `src/modules/guardrails/citation_validator.py` → `validate`; `src/modules/llm/llm_router.py` → `call`.

### 5.8 Postman

Import both files from [postman/](postman/) (*File → Import*), then pick the environment **Grounded Card Agent - local**. That gives 50 requests in 11 folders:
- health;
- the six required demo scenarios;
- guardrail edge cases;
- Idempotency-Key replay / 409 / 422;
- errors;
- retrieval;
- observability (`req_res_log`, usage and model-health summary);
- prompts;
- admin (ingest, `ingest_log`, vector store);
- semantic cache (store → hit with no LLM → different number misses → stats / clear);
- eval.

POSTs send `Idempotency-Key: {{$guid}}` automatically. Test scripts save `lastRequestId` / `conversationId` / `ingestRunId` for the follow-up requests.
To run them all from the terminal: `npx newman run postman/Grounded-Card-Agent.postman_collection.json -e postman/Grounded-Card-Agent.local.postman_environment.json`.
To regenerate after adding an endpoint: `python postman/build_collection.py`.

### 5.9 See the embedded data (ChromaDB)

Three ways:

1. **Chroma Peek viewer** (recommended):
   ```powershell
   streamlit run tools/chroma_peek/app.py       # opens http://localhost:8501
   ```
   It shows:
   - the collections (`policy_chunks` searchable, `policy_quarantine` never searched);
   - a table of every chunk: id, doc, section, version, authority, quarantine reason, token count, embedding dimension and first values, text;
   - a **2-D PCA map** of the embeddings coloured by document;
   - a **similarity query** using the same embedder as the agent.

   It's a ChromaDB 1.x-compatible take on [chroma-peek](https://github.com/Pawandeep-prog/chroma-peek). The upstream app's `pd.DataFrame(collection.get())` fails on Chroma ≥ 1.0, because `get()` now also returns an `included` list. If you prefer upstream: `git clone` it, `pip install -r requirements.txt`, `streamlit run chroma-peek/main.py`, and paste the full path of `chroma_data`. Its table view needs that one-line fix.
2. **API:** `GET /api/v1/admin/vector-store` (admin key), optionally with `?doc_id=POL-FEE-007`.
3. **Ingest output / history:** the JSON printed by `scripts.ingest`, and `GET /api/v1/admin/ingest/runs` (Postgres `ingest_log`, one row per document).

How text becomes vectors (parse → quarantine scan → chunk per section → normalise numbers → MiniLM → Chroma) is drawn in [docs/ARCHITECTURE.md §4](docs/ARCHITECTURE.md#4-how-policy-text-becomes-vectors-in-chromadb-ingest).

### 5.10 Postgres: three tables

| Table | One row per | Key columns |
|---|---|---|
| **`req_res_log`** | API call (and per eval / demo turn) | `idempotency_key` (= requestId), `source` (postman / curl / browser / python / `x-client-source` header / eval / demo), `client_ip`, `user_agent`, hashed API key + customer id, `status_code`, **`response_time_ms`**, `agent_status`, **`provider`, `model_used`**, `fallback_used`, **`llm_failures`, `models_failed`, `llm_attempts`** (every model call ok / error / duration / tokens), **`input_tokens`, `output_tokens`, `cost_usd`** (credits), `cache_hit`, `flags`, `request_body` (PII-masked), `response_body`, `spans`, `conversation_id`, `replay_count` |
| **`ingest_log`** | document per ingest run | `run_id`, `doc_id`, `action` (ingested / skipped_unchanged / quarantined / failed), `chunks`, `content_hash`, `reason`, `duration_ms` |
| **`semantic_cache`** | reusable answer | `cache_key` (sha256 of tenant + prompt + corpus + question), `embedding` (384-d), `answer`, `cited_sections`, `hit_count`, `expires_at` |

`req_res_log` also covers what used to need separate tables:
- **Idempotency:** the row is claimed at the start of a call, and its `response_body` is what a replay returns.
- **Conversation memory:** turns that share a `conversation_id`.
- **Traces:** the `spans` and `llm_attempts` columns.
- **Prompt version:** recorded on every row.

Eval runs are report files (`eval/results/`). Service-request dedupe is a 10-minute in-process map.

**APIs:**
- `GET /api/v1/requests` (filters: `source`, `provider`, `path`, `status`, `flag`);
- `GET /requests/{requestId}`, for a full row;
- `GET /requests/summary?days=7`: usage by source, LLM attempts / failure % / error types by model, answer quality by model;
- `GET /admin/ingest/runs[/{run_id}]`;
- `GET|DELETE /admin/cache`.

Or query directly (pgAdmin, DBeaver, psql):

```sql
-- who uses how much
SELECT source, count(*) calls, sum(input_tokens + output_tokens) tokens, sum(cost_usd) cost,
       round(avg(response_time_ms)) avg_ms, sum(cache_hit::int) cache_hits
FROM req_res_log GROUP BY source ORDER BY calls DESC;
-- which model fails, and how
SELECT a->>'model' model, count(*) attempts, count(*) FILTER (WHERE NOT (a->>'ok')::bool) failed, a->>'error' error
FROM req_res_log, jsonb_array_elements(llm_attempts) a GROUP BY 1, 4 ORDER BY failed DESC;
```

### 5.11 Semantic cache: answers from the database, not the LLM

A repeated or paraphrased **account-independent** question is answered from `semantic_cache` in about 0.4 s with **zero LLM calls** (measured live; 2–23 s with the model). The lookup goes:
1. **Exact key:** sha256 of tenant, prompt version, corpus hash and the normalised question (punctuation-insensitive).
2. **Semantic match:** cosine ≥ `SEMANTIC_CACHE_MIN_SIM` (0.87) **and** identical numbers in both questions.

Every hit is re-checked for the asking customer before it's served:
- the cited policy sections must be retrieved again for the new question;
- the customer must exist (404 otherwise);
- the output guard must pass (for example, no product talk to a vulnerable customer).

**What gets stored:** only answers that are validated, needed no repair, used no account data, raised no service request, had no input flags and no conversation history.

**Invalidation:** a new prompt version or a re-ingested corpus can never match old entries (ingest also deletes them); entries expire after 24 h; `DELETE /api/v1/admin/cache` clears them by hand.

**Scaling, the next phase: Redis.** Today the semantic match is a numpy scan over at most 2,000 rows in one API process, because Postgres has no vector index here. With several workers or nodes, or a large cache, move it to **Redis**:
- a RediSearch HNSW vector index (`KNN` query) for the semantic match;
- a native TTL per key;
- sub-millisecond reads shared by all workers.

Only `lookup()` / `store()` in `src/modules/cache/semantic_cache.py` change. pgvector is the alternative if it should stay in Postgres. The other in-process state goes to Redis in the same step: rate limits, circuit breakers, service-request dedupe, and the embedding / retrieval caches.

### 5.12 Logs

**Log file:** `logs/<dd-mm-yyyy>output.log` (a new file each day), one JSON object per line:

```json
{"date": "2026-11-14 23:01:26.182", "type": "error",
 "message": "Grounded-Card-RAG | src/controller | chat_controller.py | worker | requestId: e2e-stream-0001 | message: chat_stream_failed",
 "stack": "Traceback (most recent call last): ..."}
```

`type` is `info`, `warn` or `error`; errors carry the full `stack`. Find everything about one request with `Select-String -Path logs\*.log -Pattern "<requestId>"` (macOS/Linux: `grep`). Uvicorn's own start/stop/crash lines go to the same file. Customer messages, names, scores and keys are never logged: ids are hashed, PII is masked first.

### 5.13 Tests, demo, eval

```powershell
pytest -q                        # 129 tests, ~2 s, no DB / API key needed
ruff check .
python -m scripts.demo           # the 6 required scenarios with span trees
python -m eval.run_eval          # 32 cases -> eval/results/run-*.md ; exit 1 if the gate fails
python -m eval.run_eval --cases EV-001,EV-007 --pause 20   # subset; --pause spaces calls on free tiers
```

Toggles: `TOOL_FAIL_RATE=1` (forced account-tool timeout → `degraded`), `LLM_FORCE_PRIMARY_FAIL=1` (Groq/Grok fallback), `LLM_DISABLED=1` (static mode).

### 5.14 Troubleshooting

| Symptom | Fix |
|---|---|
| `connection refused` / `timeout` on 5433 | Postgres not running: `pg_isready -p 5433`; Windows: *Services → postgresql-x64-18 → Start*, or register the service (§5.2). |
| pgAdmin shows no `agent` tables | pgAdmin points at a different server/port than `DATABASE_URL`, or migrate hasn't run. Both must use 5433. |
| Settings vanished from `.env` | An editor tab with an old copy of `.env` was saved over it. Close stale tabs and compare with `.env.example`. |
| `password authentication failed for user "agent"` | Create the role (§5.2) or fix `DATABASE_URL`. |
| `/ready` → 503 with `corpus_ingested: false` | Run `python -m scripts.ingest`. |
| Every answer is `degraded` + `llm_unavailable` | Keys missing or quotas used up: check `"llm_chain"` in `/ready` and `llm_call_failed` lines in the log (429 = quota). Free Gemini quotas reset daily; Groq's free tier is 8k tokens/min per model. |
| `400 IDEMPOTENCY_KEY_REQUIRED` | Add the `Idempotency-Key` header to the POST. |
| `422 IDEMPOTENCY_KEY_REUSED` | Same key, different body: use a new key. |
| A cached answer looks wrong | `DELETE /api/v1/admin/cache`, raise `SEMANTIC_CACHE_MIN_SIM`, or set `ANSWER_CACHE_TTL_MS=0` to switch the cache off. |
| First ingest is slow / fails offline | It downloads the embedding model once; needs internet the first time. |
| Port 3000 busy | Set `PORT` in `.env`, or `--port` in the launch config. |

### 5.15 Project layout

```
src/
  app.py, server.py            FastAPI app + uvicorn entry point
  routes/  controller/         URL -> handler -> HTTP shaping
  middleware/                  request context + logging, idempotency, timeout, auth, rate limit, validation, errors
  modules/
    agent/                     the tool-calling loop, tool schemas, respond schema, safe responses
    retrieval/                 chunker, embedder, BM25 (keyword_search), vector_search, RRF fuse, retriever
    guardrails/                input_guard + pii (entry), citation_validator + output_guard (exit), redact
    llm/                       gemini_client, grok_client (Groq/xAI), llm_router, errors, pricing
    decision/                  facts + limit-increase rules (policy as code)
    tools/                     the supplied tools + runner (timeouts, retries, breaker, idempotency)
    telemetry/                 file logger, tracer, metrics
    cache/                     semantic_cache (Postgres answer cache), in-process LRU (embeddings, retrieval)
  db/                          chroma.py (vector store), index.py (Postgres pool), migrations/ (3 tables),
                               repositories/ (req_res_log, ingest_log, semantic_cache, policy = Chroma)
  const/ helper/               config, enums, reason codes, conflicts; text / money / date helpers
scripts/                       migrate, ingest, demo
eval/                          eval cases, judges, runner, last report + demo output
prompts/                       system.v1..v5.md (never edited in place)
tools/chroma_peek/             vector-store viewer
postman/                       collection + environment (+ generator)
docs/ARCHITECTURE.md           flow diagrams, ingest pipeline, guardrail tables
tests/                         129 tests
data/                          policy corpus, accounts, tool contracts, seed eval
```
