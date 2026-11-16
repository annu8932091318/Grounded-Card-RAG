# Architecture and flow

The diagrams are Mermaid. They render on GitHub, and in VS Code with the *Markdown Preview Mermaid Support* extension (`Ctrl+Shift+V`).
Every box names the file that implements it.

## 1. The big picture

```mermaid
flowchart LR
    client["Client<br/>Postman · curl · web app"] -->|"HTTP + x-api-key<br/>+ Idempotency-Key (POST)"| api

    subgraph api["FastAPI app (src/app.py), runs locally with uvicorn"]
        mw["Middleware<br/>request context → idempotency → timeout"] --> routes["Routes /api/v1/*"] --> ctrl["Controllers"]
        ctrl --> agent["Agent loop<br/>src/modules/agent/agent.py"]
    end

    agent --> guard_in["Entry guardrail<br/>sanitize · PII mask · flags"]
    agent --> cache["Semantic cache<br/>modules/cache/semantic_cache.py"]
    agent --> retr["Hybrid retrieval<br/>BM25 + vector + RRF"]
    agent --> tools["Tools<br/>account state · eligibility rules · service request"]
    agent --> router["LLM router"]
    agent --> guard_out["Exit guardrails<br/>citation validator · output guard"]

    retr --> chroma[("ChromaDB<br/>./chroma_data<br/>policy vectors")]
    router -->|primary| gemini["Google Gemini<br/>(models in GEMINI_MODEL)"]
    router -->|fallback| groq["Groq / xAI Grok<br/>(OpenAI-compatible)"]
    router -.->|all down| static["Static mode (no LLM)<br/>account lookups from the record ·<br/>only relevant policy lines · else handoff"]

    cache --> pg
    api --> pg[("PostgreSQL :5433<br/>req_res_log · ingest_log · semantic_cache")]
    api --> logs[["logs/dd-mm-yyyy output.log"]]
```

| Store | What lives there | Why there |
|---|---|---|
| **ChromaDB** (`./chroma_data`, embedded, no server) | policy chunks + metadata, **one collection per embedding model** (Gemini 001 768-d, Gemini 2 768-d, local bge-small 384-d); quarantined docs in a separate collection | vector similarity search (HNSW, cosine) |
| **PostgreSQL** (local install, port 5433) | exactly **3 tables**: `req_res_log`, `ingest_log`, `semantic_cache` (§7) | relational, queryable: who called, from where, which model answered, what it cost; ingest audit; reusable answers |
| **Files** | prompts (`prompts/system.vN.md`), eval reports (`eval/results/run-*.json/.md`) | versioned / diffable artefacts, not records you query |
| **Log file** (`logs/14-11-2026output.log`) | every event, one JSON line, with file / function / requestId | grep-able debugging when something goes wrong |

## 2. One `/chat` request, end to end

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant M as Middleware
    participant A as Agent (agent.py)
    participant G as Entry guard
    participant R as Retriever
    participant L as LLM router
    participant T as Tools
    participant V as Exit guards
    participant P as Postgres

    C->>M: POST /api/v1/chat {customer_id, message}<br/>x-api-key, Idempotency-Key
    M->>M: requestId = Idempotency-Key → log context
    M->>P: claim req_res_log row for the Idempotency-Key (replay if already completed)
    M->>A: auth ✓ rate limit ✓ schema ✓ → run_agent()
    A->>G: sanitize + mask PII + flag injection / score / cross-customer
    A->>P: semantic_cache lookup (exact key, then cosine ≥ threshold[embedding model] + same numbers)
    alt cache hit, re-checked for this request
        A-->>M: cached answer (no LLM): citations re-retrieved, output guard for this customer
    end
    A->>R: search_policy(question) — before the first model call
    R-->>A: policy excerpts (or NO_RELEVANT_POLICY_FOUND)
    loop max 6 iterations, 25 s budget
        A->>L: system prompt + tools + messages
        L-->>A: tool calls (Gemini; Groq/Grok if Gemini fails)
        A->>T: get_account_state / check_limit_increase_eligibility / raise_service_request / search_policy
        T-->>A: results (customer_id bound server-side, internal score redacted)
    end
    A->>V: respond(payload) → citation validator → output guard
    alt invalid
        V-->>A: errors → ONE repair turn → still invalid → safe response
    end
    A->>P: store answer in semantic_cache (only if account-independent and clean)
    A-->>M: structured answer + trace (LLM attempts, tokens, spans)
    M->>P: complete the req_res_log row: response, response time, model, tokens, cost, failures
    M-->>C: 200 {status, customer_message, claims[citations], decision, routing, meta{provider, model}}
```

## 3. The LLM fallback chain

```mermaid
flowchart TD
    start(["llm_router.call()"]) --> g1{"Gemini model 1<br/>breaker closed?"}
    g1 -->|yes| c1["call (6 s timeout)"]
    c1 -->|ok| done(["return response<br/>provider=gemini"])
    c1 -->|"timeout / 429 quota"| g2
    c1 -->|"5xx / network"| r1["retry once<br/>(backoff)"] --> g2
    c1 -->|"401/403 bad key"| open["open breaker 10 min"] --> g2
    g1 -->|no| g2{"Gemini model 2, 3 ..."}
    g2 -->|all failed| k1{"Groq / Grok model 1, 2"}
    k1 -->|ok| done2(["return response<br/>provider=groq · fallback_used"])
    k1 -->|all failed| s(["LLMUnavailable → static mode:<br/>1 flagged → refuse · 2 account lookup → from the record<br/>3 action request → 'can't do that now'<br/>4 only policy lines covering the question · else nothing<br/>+ routed to a human"])
```

`LLM_FORCE_PRIMARY_FAIL=1` simulates a Google outage (to demo the fallback). `LLM_DISABLED=1` goes straight to static mode.

## 4. How policy text becomes vectors in ChromaDB (ingest)

```mermaid
flowchart LR
    md["data/policy_corpus/*.md"] --> parse["parse_doc()<br/>title, doc_id, version,<br/>effective date, owner"]
    parse --> hash{"content_hash<br/>unchanged?"}
    hash -->|yes| skip["skip (idempotent)"]
    hash -->|no| scan{"scan_corpus_doc()<br/>injection / draft markers?"}
    scan -->|"clean"| chunk["chunk_doc()<br/>one chunk per ## section<br/>(fee table kept whole)"]
    scan -->|"flagged (POL-OPS-012)"| chunkq["chunk_doc()"]
    chunk --> norm["normalise()<br/>4,00,000 → 400000<br/>3.5% → 3.5 percent"]
    norm --> e1["gemini-embedding-001<br/>API, RETRIEVAL_DOCUMENT, 768-d"] --> c1[("policy_chunks__gemini_embedding_001")]
    norm --> e2["gemini-embedding-2<br/>API, 768-d"] --> c2[("policy_chunks__gemini_embedding_2")]
    norm --> e3["local bge-small-en-v1.5<br/>fastembed CPU, 384-d"] --> c3[("policy_chunks__local_BAAI_bge_small_en_v1_5")]
    chunkq --> embq["local bge-small only<br/>(never searched: no API quota)"] --> q[("policy_quarantine<br/>never searched")]
    c1 & c2 & c3 --> runs[("Postgres ingest_log<br/>one row per document per model")]
    runs -.->|corpus changed| sc[("semantic_cache:<br/>stale answers deleted")]
```

Each Chroma record:

| field | example |
|---|---|
| `id` | `default:POL-FEE-007:Table` (tenant:doc:section; re-ingest replaces in place) |
| `document` | `POL-FEE-007 Fees and Charges Schedule §Table: \| Charge \| Amount \| ...` (shown, quoted, cited) |
| `embedding` | 768 floats (Gemini) or 384 (bge-small), unit length |
| `metadata` | `doc_id, tenant_id, section, title, version, owner, effective_date, authority (policy/non_policy), quarantined, quarantine_reason, content_hash, text_norm, token_count, embedding_model` |

**How to see it:** `streamlit run tools/chroma_peek/app.py` (table, embedding map, live similarity query),
`GET /api/v1/admin/vector-store`, or `python -m scripts.ingest` output. Details are in the README section *See the embedded data*.

## 5. Hybrid retrieval

```mermaid
flowchart LR
    q["question"] --> n["normalise()"]
    n --> bm["BM25 leg<br/>keyword_search.py<br/>in-memory index over Chroma chunks"]
    n --> ch["embedding chain<br/>gemini-embedding-001 → gemini-embedding-2 → local bge-small"]
    ch --> ve["Vector leg<br/>that model's Chroma collection<br/>cosine, where tenant + authority=policy"]
    bm --> f["RRF fusion k=60<br/>+ each leg's #1 always kept"]
    ve --> f
    n --> gm["gate similarity<br/>local bge-small (always available)"]
    f --> gate{"relevance gate<br/>bge-small sim ≥ 0.65, or ≥ 0.55 + keyword coverage ≥ ⅓<br/>OR explicit doc id"}
    gm --> gate
    gate -->|grounded| conf["attach conflict notes<br/>D1: CRD-001 prevails over CRD-011"] --> out["&lt;policy_excerpt&gt; blocks (data, not instructions)"]
    gate -->|not grounded| miss["NO_RELEVANT_POLICY_FOUND<br/>→ answer_with_gap / refuse + route to GENERAL"]
```

### Embeddings: which model does what (measured on this corpus)

| Measured | gemini-embedding-001 | gemini-embedding-2 | local bge-small-en-v1.5 |
|---|---|---|---|
| Right policy in top 5, hybrid (39 questions incl. paraphrases) | **34** | **34** | not re-measured (MiniLM: 29) |
| Right policy ranked #1, vector | **32** | 28 | not re-measured (MiniLM: 23) |
| Paraphrase ↔ "different detail" similarity (cache) | **0.89–0.92 ↔ 0.81** | 0.90–0.94 ↔ 0.86 | 0.86–0.94 ↔ 0.85 |
| Gate: uncovered questions grounded (of 11) | 4 (0 covered refused) | 9 (0 covered refused) | **2** (2/26 raw covered refused, both grounded after the agent's reformulation; 0.65/0.55) |

So each job uses the model that is best at it:
- **Ranking + semantic cache:** the chain gemini-embedding-001 → gemini-embedding-2 → local bge-small. The first model that answers is used, and that model's own collection.
- **"Is this covered at all?":** the relevance gate always uses local bge-small (it replaced all-MiniLM-L6-v2 on 2026-11-15). It runs in milliseconds, can't run out of quota, and its thresholds were measured on 53 probes; like Gemini it compresses scores (unrelated questions 0.45–0.65), so its margin is thin.
- **When things fail:**
  - Gemini quota or credit exhausted: next Gemini model, then local bge-small. Each has its own quota and collection, and the trace flag shows `embedding_fallback:<model>`.
  - Everything down: BM25 only (keyword coverage ≥ 0.5) and exact-key cache hits; flag `embedding_unavailable`.
- **Keyword half of the gate:** the share of the question's own specific terms (≥ 3 letters, not generic) found in the top BM25 hit. The old "any term matches" rule grounded 6 of 11 uncovered questions; the token "s" from "what's" matched.

## 6. Guardrails: entry and exit

### Entry (before anything reaches the model)

| # | Guardrail | Where | What it stops |
|---|---|---|---|
| 1 | API key (constant-time compare), admin-only routes | `middleware/auth_middleware.py` | anonymous use, users calling admin endpoints |
| 2 | Idempotency-Key required on POST, replay / 409 / 422 | `middleware/idempotency_middleware.py` | duplicate side effects (double service requests) on client retries |
| 3 | Rate limit, 60/min per key | `middleware/rate_limit_middleware.py` | abuse, runaway cost |
| 4 | Schema validation (`extra=forbid`, `CUST-dddd`, 1–1000 chars) | `middleware/validate_middleware.py` | malformed / oversized input |
| 5 | Request timeout 30 s (agent budget 25 s) | `middleware/timeout_middleware.py` | hung requests |
| 6 | Sanitize: strip control, zero-width and bidi characters | `guardrails/input_guard.py` | hidden-text injection, log forging |
| 7 | PII masking: card, Aadhaar, PAN, e-mail, phone, OTP/CVV/PIN | `guardrails/pii.py` | PII reaching the LLM provider, Postgres or logs |
| 8 | Flags: injection, internal-score request, cross-customer id | `guardrails/input_guard.py` | (flags are *enforced* later by the validator) |
| 8b | **Scope check, two tiers, from the corpus only**: off-domain iff the mean similarity of the 3 nearest policy chunks (local bge-small) < 0.475 **and** < 25% of the question's words occur in the policy documents. Off-domain → fixed reply, no LLM, no ticket. In domain but uncovered → normal path (honest gap + GENERAL ticket). Skipped in a conversation (follow-ups need their history); fails open. Measured: 0/40 in-domain blocked, 14/16 off-domain caught | `guardrails/scope_guard.py`, `agent.py::_out_of_scope` | LLM cost and pointless human tickets for "write me a poem" |
| 9 | customer_id bound server-side; no tool takes a customer id; no "approve" tool exists | `agent/tools_schema.py`, `tools/tool_runner.py` | reading another customer's data, approving anything |
| 10 | Internal score + name redacted before the model sees the account | `guardrails/redact.py` | score leakage at the source |
| 11 | Corpus quarantine at ingest (separate collection) | `scripts/ingest.py`, `db/chroma.py` | indirect prompt injection from documents (POL-OPS-012) |
| 12 | Retrieved text wrapped as `<policy_excerpt>` data | `retrieval/retriever.py` + prompt | instructions hidden inside documents |

### Exit (before anything reaches the customer)

| # | Guardrail | Where | What it stops |
|---|---|---|---|
| 1 | Schema check of the `respond` payload | `guardrails/citation_validator.py` | unstructured / malformed answers |
| 2 | Every claim cited; cited doc+section must have been retrieved **this turn** | same | invented sources, citing the quarantined doc |
| 3 | Quotes must be verbatim (else dropped, never shown) | same | fake quotations |
| 4 | Every number in a claim must appear in a cited source or computed fact | same | invented fees, rates, dates, multipliers |
| 5 | Reason codes only AA01–AA06 and only those the rules engine returned | same | invented / misapplied decline reasons |
| 6 | Status consistency; injection / cross-customer ⇒ `refused`; a decline cites its rule doc; a raised request cites POL-SVC-006 | same | policy behaviour left to the model's discretion |
| 7 | Conflicts: never rely on the losing doc alone (D1/D5) | same | superseded policy |
| 8 | Output guard: internal score, approval language, sales to vulnerable customers, flag disclosure, prompt leak, OPS-012, other customers' ids, **PII** | `guardrails/output_guard.py` | leaks and forbidden content |
| 9 | One repair turn, then a safe response (never a guess) | `agent/agent.py`, `agent/safe_responses.py` | shipping an unverified answer |
| 10 | Request bookkeeping from the real tool result (request id, queue, SLA) | `agent/agent.py::_complete_routing` | invented reference numbers |
| 11 | Error envelope without stack traces | `middleware/error_middleware.py` | internal detail leaking in errors |
| 12 | **Static-mode relevance rules** (no LLM to judge): flagged → refuse; plain account lookup → from the record; action request → no excerpt; otherwise only the policy lines covering ≥ ⅔ of the question's key terms (terms unknown to the corpus ignored), else no excerpt | `agent/static_answers.py`, `agent/safe_responses.py::static_mode` | irrelevant policy dumped as an "answer" (found live: "When is my payment due?" returned the whole fee table) |

## 7. Postgres: three tables

```mermaid
erDiagram
    req_res_log {
        text idempotency_key "UNIQUE with tenant_id = requestId"
        text state "in_progress | completed | failed"
        text source "postman | curl | x-client-source | eval | demo"
        int response_time_ms
        text provider "gemini | groq | grok"
        text model_used
        int llm_failures
        text_array models_failed
        jsonb llm_attempts "every model call ok / error"
        int input_tokens
        int output_tokens
        numeric cost_usd
        bool cache_hit
        jsonb request_body "PII masked"
        jsonb response_body "replayed for a repeated key"
        jsonb spans
        uuid conversation_id "conversation memory"
        int replay_count
    }
    ingest_log {
        uuid run_id
        text doc_id
        text action "ingested | skipped_unchanged | quarantined | failed"
        int chunks
        text content_hash
        text reason
    }
    semantic_cache {
        text cache_key PK "sha256(tenant|prompt|corpus|question)"
        real_array embedding "768-d Gemini or 384-d bge-small"
        text embedding_model "compared only within one model"
        jsonb answer
        text_array cited_sections
        text corpus_hash
        text prompt_version
        int hit_count
        timestamptz expires_at
    }
```

| Table | One row per | Answers the question |
|---|---|---|
| `req_res_log` | API call (and per eval/demo turn) | Who called, from where, with which key? How long did it take? Which provider/model answered, which failed and why, how many tokens/credits? Was it a cache hit or a replay? What exactly was asked (masked) and answered? |
| `ingest_log` | document per ingest run | What was embedded into Chroma, when, what was skipped or quarantined, what failed? |
| `semantic_cache` | reusable answer | Can this question be answered from the database instead of the LLM? |

It also does the work of the tables that were removed:
- **Idempotency:** the claimed `req_res_log` row is the lock, and its `response_body` is what a replay returns.
- **Conversation memory:** the last turns with the same `conversation_id` and customer hash.
- **Traces:** the `spans` and `llm_attempts` columns.
- **Prompt versions:** `prompt_version` and `prompt_sha` on every row, with the prompt text in files.
- **Eval runs:** report files.
- **Service-request dedupe:** a 10-minute in-process map.

## 8. Semantic cache: answering from the database

```mermaid
flowchart TD
    q["question (PII-masked)"] --> el{"cacheable request?<br/>no input flags, no conversation history"}
    el -->|no| llm
    el -->|yes| ex{"exact key<br/>sha256(tenant·prompt·corpus·question)"}
    ex -->|hit| chk
    ex -->|miss| sem{"cosine(question, stored, same embedding model)<br/>≥ threshold[model] AND identical numbers"}
    sem -->|hit| chk
    sem -->|miss| llm["agent loop + LLM"]
    chk{"re-check for THIS request<br/>1 cited sections retrieved again (local)<br/>2 customer exists (404) and output guard passes"}
    chk -->|pass| ans(["answer, cache_hit=true, llm_calls=0, ~0.4 s"])
    chk -->|fail| llm
    llm --> v{"answered, validated, no repair,<br/>no account data, no service request?"}
    v -->|yes| store[("INSERT semantic_cache<br/>TTL 24 h")]
    v -->|no| done(["answer, not stored"])
    store --> done
```

* **Thresholds are per embedding model** (their scales differ), measured on "how much is the foreign currency markup?":

  | Compared with | gemini-embedding-001 | gemini-embedding-2 | bge-small |
  |---|---|---|---|
  | "what is foreign currency markup?" (paraphrase) | 0.918 | 0.940 | 0.938 |
  | "forex markup charges?" (paraphrase) | 0.892 | 0.904 | 0.861 (misses: below 0.90) |
  | "…markup on my Classic card?" (different detail) | 0.814 | 0.861 | 0.849 |
  | "What is the late payment fee?" (different question) | 0.561 | 0.714 | 0.678 ("annual fee?") |
  | **threshold** | **0.86** | **0.89** | **0.90** |

  An entry is only compared with questions embedded by the **same** model (`semantic_cache.embedding_model`).
* **Why not the embedding as the primary key:** float vectors never compare equal, so the key would only match the identical question. The hash key gives an O(1) exact path; the stored embedding gives the semantic path.
* **Invalidation:**
  - **New prompt version:** a different scope, so old entries are never matched.
  - **Re-ingested corpus:** the corpus hash changes, and ingest deletes the stale rows.
  - **Expiry:** `expires_at` (24 h).
  - **By hand:** `DELETE /admin/cache`.
* **Next phase: Redis.** Today the cosine scan runs in Python over at most 2,000 rows per lookup, inside one API process, and Postgres has no vector index. With several API workers or nodes, or a large cache, move it to **Redis**:
  - RediSearch HNSW vector index (`FT.SEARCH … KNN`) for the semantic match;
  - a native TTL per key;
  - sub-millisecond reads, shared by every worker.

  The lookup and store functions in `semantic_cache.py` are the only code to swap. pgvector is the alternative if it should stay in Postgres.
  The same move applies to the other in-process state: rate-limit windows, circuit breakers, service-request dedupe, and the embedding / retrieval LRU caches.

## 9. Logging

* **File log**: `logs/<dd-mm-yyyy>output.log`, one JSON object per line:
  ```json
  {"date": "2026-11-14 23:01:26.182", "type": "error",
   "message": "Grounded-Card-RAG | src/controller | chat_controller.py | worker | requestId: e2e-stream-0001 | message: chat_stream_failed",
   "stack": "Traceback (most recent call last): ..."}
  ```
  `requestId` is the call's Idempotency-Key (or `x-request-id`, or a generated uuid). The same id appears in four places, so it ties them together:
  - `req_res_log.idempotency_key`;
  - the `x-request-id` response header;
  - every log line of that call;
  - the error envelope.
* **`GET /api/v1/requests/summary`** reads `req_res_log` for usage by source, failures by model and answer quality by model. **`GET /api/v1/requests/{requestId}`** returns the full row: request, response, LLM attempts and spans.

## 10. Low-level design

Every check a request passes through, in order: its failure code, and the database reads and writes it makes. Numbers are
the live defaults from `src/const/config.py` (all overridable in `.env`).

### 10.1 HTTP layer: middleware and route dependencies

Starlette runs the last-added middleware first, so the order is request context → idempotency → timeout → route
dependencies (auth → rate limit → body validation) → controller.

```mermaid
flowchart TD
    IN(["HTTP request<br/>x-api-key · Idempotency-Key · x-client-source · x-request-id"]) --> RC

    subgraph M1["① request_context_middleware.py (outermost)"]
        RC["requestId = Idempotency-Key ‖ x-request-id (safe chars, ≤128) ‖ uuid4<br/>source = x-client-source ‖ derived from User-Agent<br/>ContextVar requestId → every log line · log request_started"]
    end

    RC --> IDEM

    subgraph M2["② idempotency_middleware.py (POST only)"]
        IDEM{"Idempotency-Key present?"} -->|no| E400a["400 IDEMPOTENCY_KEY_REQUIRED"]
        IDEM -->|yes| FMT{"8–128 chars of A-Z a-z 0-9 . _ : - ?"}
        FMT -->|no| E400b["400 IDEMPOTENCY_KEY_INVALID"]
        FMT -->|yes| BODY["read body · sha256 · PII-mask → request.state.request_body<br/>tenant = tenant_for_key(x-api-key)"]
        BODY --> TEN{"key maps to a tenant?"}
        TEN -->|no| PASS1["skip (auth returns 401)"]
        TEN -->|yes| CLAIM[("req_res_log<br/>INSERT … state='in_progress'<br/>ON CONFLICT (tenant_id, idempotency_key)<br/>DO UPDATE WHERE state='failed' OR older than 24 h")]
        CLAIM -->|claimed| NEXT2["continue"]
        CLAIM -->|row exists| EX{"same body sha + path?"}
        EX -->|no| E422["422 IDEMPOTENCY_KEY_REUSED"]
        EX -->|yes, in_progress| E409["409 IDEMPOTENCY_IN_PROGRESS"]
        EX -->|yes, completed| REPLAY["200 stored response_body<br/>header Idempotent-Replayed: true<br/>UPDATE replay_count += 1"]
        CLAIM -.->|Postgres down| DBDOWN["log idempotency_unavailable<br/>continue without replay protection"]
    end

    NEXT2 --> TO
    DBDOWN --> TO

    subgraph M3["③ timeout_middleware.py"]
        TO["asyncio.wait_for(…, 30 s)  (not on /eval/run)"] -->|exceeded| E504["504 TIMEOUT"]
    end

    TO --> AUTH

    subgraph D["④ route dependencies (FastAPI Depends)"]
        AUTH{"require_api_key: x-api-key matches API_KEYS<br/>(hmac.compare_digest, constant time)"} -->|no| E401["401 UNAUTHORIZED"]
        AUTH -->|admin routes: require_admin| ADM{"admin key?"}
        ADM -->|user key| E403["403 FORBIDDEN"]
        AUTH -->|yes| RL{"rate_limit: ≤ 60 req / 60 s per key<br/>(sliding window, in-process)"}
        RL -->|no| E429["429 RATE_LIMITED"]
        RL -->|yes| VAL{"pydantic, extra=forbid<br/>customer_id = CUST + 4 digits · message 1–1000 chars<br/>conversation_id uuid · top_k 1–20"}
        VAL -->|no| E400c["400 VALIDATION_ERROR + field names (no input echo)"]
    end

    VAL -->|ok| CTRL["⑤ controller → agent.run_agent()  (§10.2)"]
    CTRL --> OUT

    subgraph M4["① again, when the response body has finished streaming"]
        OUT[("req_res_log UPSERT: state completed | failed (5xx / 429),<br/>status_code, response_time_ms, response_body,<br/>agent fields: provider, model, llm_attempts, tokens, cost, flags, spans")] --> LOGF[["logs/dd-mm-yyyy output.log: request_finished"]]
    end

    E400a & E400b & E422 & E409 & E504 & E401 & E403 & E429 & E400c --> ENV["error_middleware.py: {error: {code, message, request_id}}<br/>never a stack trace (it goes to the log file)"]
```

### 10.2 Agent pipeline: entry guards → answer → exit guards

```mermaid
flowchart TD
    S(["run_agent(customer_id, message, tenant, conversation_id)"]) --> G1

    subgraph ENTRY["ENTRY guardrails"]
        G1["input_guard.sanitize: strip control / zero-width / bidi chars"] --> G2["pii.mask: card · Aadhaar · PAN · e-mail · phone · OTP/CVV/PIN → [TOKENS]<br/>flag pii_masked"]
        G2 --> G3["input_guard.check flags: injection_suspected · internal_score_request · cross_customer_request"]
        G3 --> CONV[("req_res_log READ: conversation_owner(conversation_id)<br/>+ last 2 turns of this customer → history<br/>foreign / unknown id → new conversation")]
        CONV --> SC{"scope_guard (no history):<br/>top-3 sim (bge-small) < 0.475 AND corpus-word share < 0.25 ?"}
        SC -->|off-domain| OOS["fixed reply · status refused · flag out_of_scope<br/>no LLM · no ticket"]
    end

    SC -->|in scope| CA

    subgraph CACHE["semantic cache (no input flags, no history)"]
        CA[("semantic_cache READ: exact key sha256(tenant·prompt·corpus·question)")] -->|miss| CB["embed question (chain) → cosine vs entries of the SAME model<br/>≥ 0.86 (001) / 0.89 (-2) / 0.90 (bge) AND identical numbers"]
        CA -->|hit| RECHECK
        CB -->|hit| RECHECK{"re-check for THIS request:<br/>cited sections retrieved again (local) ·<br/>customer exists (else 404) · output guard passes"}
        RECHECK -->|pass| HIT["answer from DB · cache_hit · llm_calls 0<br/>semantic_cache UPDATE hit_count"]
    end

    CB -->|miss| RET
    RECHECK -->|fail| RET

    subgraph LOOP["agent loop: ≤ 6 iterations, 25 s budget"]
        RET["search_policy(question) up front:<br/>BM25 + vector (embedding chain 001 → 2 → bge) · RRF k=60 · top 5<br/>gate (bge): sim ≥ 0.65, or ≥ 0.55 + keyword coverage ≥ ⅓"] --> LLM["llm_router: Gemini models → Groq models<br/>timeout / 429 → next model · short Retry-After → wait (≤ 12 s, within budget)<br/>5xx → 1 retry · bad key → breaker 10 min"]
        LLM -->|tool calls| TOOLS["tools (customer_id bound server-side):<br/>get_account_state (2 s timeout, 2 retries, breaker; score + name redacted)<br/>check_limit_increase_eligibility (policy as code)<br/>raise_service_request (no blind retry; 10-min dedupe)<br/>search_policy (again, model's own wording)"]
        TOOLS --> LLM
        LLM -->|all models fail| STATIC["static mode: refuse flagged · account lookup from record ·<br/>action → handoff · only relevant policy lines · else handoff"]
    end

    LLM -->|respond| V

    subgraph EXIT["EXIT guardrails"]
        V["citation_validator: schema · every claim cited · doc+section retrieved THIS turn ·<br/>quotes verbatim (else dropped) · every number in a cited source ·<br/>reason code ∈ AA01–AA06 and supported by rules · status consistency ·<br/>injection → refused · decline cites its rule doc · conflicts D1/D5"] -->|errors| REP{"first failure?"}
        REP -->|yes| LLM
        REP -->|no| SAFE["safe response: refused, routed to GENERAL"]
        V -->|ok| OG["output_guard: score leak · approval language · sales to vulnerable ·<br/>flag disclosure · prompt leak · OPS-012 · other customer ids · PII"]
        OG -->|violation| REP
    end

    OG -->|ok| FIN["_complete_routing / _finalize: routing + SLA from the real tool result ·<br/>gap → GENERAL ticket · customer_message from validated claims only"]
    FIN --> STORE[("semantic_cache INSERT if cacheable:<br/>answered · no account data · no ticket · no flags · no repair")]
    FIN --> TR["tracer.end → metrics + log request_completed<br/>(HTTP: middleware writes req_res_log · scripts: tracer writes it)"]
    OOS & HIT & STATIC & SAFE --> TR
```

### 10.3 Database schema (Postgres, 3 tables) and vector store (ChromaDB)

```mermaid
erDiagram
    req_res_log {
        bigserial id PK
        text idempotency_key "UNIQUE (tenant_id, idempotency_key) = requestId"
        text tenant_id "default anonymous"
        text state "in_progress | completed | failed"
        text method
        text path
        text source "postman | curl | browser | python | x-client-source | eval | demo"
        text client_ip
        text user_agent
        text api_key_hash "sha256 prefix, never the key"
        text customer_id_hash "sha256 prefix, never the id"
        uuid conversation_id "conversation memory"
        text request_body_sha256 "same key + other body -> 422"
        jsonb request_body "PII-masked, customer id hashed"
        jsonb response_body "replayed for a repeated key"
        int status_code
        text error_code
        text agent_status "answered | answered_with_gap | declined | refused | request_raised | degraded"
        int response_time_ms
        text provider "gemini | groq | grok | null"
        text model_used
        bool fallback_used
        bool cache_hit
        int llm_calls
        int llm_failures
        text_array models_failed
        jsonb llm_attempts "provider, model, attempt, ok, error, duration_ms, tokens"
        int input_tokens
        int output_tokens
        int cache_read_tokens
        numeric cost_usd
        text prompt_version
        text prompt_sha
        text_array flags "out_of_scope, cache_hit_semantic, fallback_used, static_mode, pii_masked, ..."
        uuid trace_id
        jsonb spans "the agent's step tree"
        int replay_count
        timestamptz created_at
        timestamptz completed_at
    }
    ingest_log {
        bigserial id PK
        uuid run_id "one ingest run"
        text tenant_id
        text doc_id
        text file_name
        text embedding_model "one row per doc per model"
        text action "ingested | skipped_unchanged | quarantined | failed"
        int chunks
        text content_hash
        text version
        text reason "quarantine reason / error"
        int duration_ms
        text chroma_path
        timestamptz created_at
    }
    semantic_cache {
        text cache_key PK "sha256(tenant | prompt | corpus | question)"
        text tenant_id
        text prompt_version
        text corpus_hash "stale after ingest"
        text question_norm "PII-masked"
        real_array embedding "768-d Gemini or 384-d bge"
        text embedding_model "compared only within one model"
        jsonb answer "validated respond payload"
        text_array cited_sections "re-checked on every hit"
        text model_used
        int hit_count
        timestamptz created_at
        timestamptz last_hit_at
        timestamptz expires_at "24 h"
    }
    chroma_policy_chunks {
        text id PK "tenant:doc_id:section (same in every model collection)"
        text document "chunk text: shown, quoted, cited"
        vector embedding "one collection per embedding model"
        text doc_id
        text section
        text authority "policy"
        text content_hash
        text embedding_model
    }
    chroma_policy_quarantine {
        text id PK
        text document "POL-OPS-012: audit only, never searched"
        text quarantine_reason
    }
    ingest_log }o--o{ chroma_policy_chunks : "records what was written (doc_id + embedding_model)"
    ingest_log }o--o{ chroma_policy_quarantine : "records quarantined docs"
    semantic_cache }o--o{ chroma_policy_chunks : "cited_sections must be re-retrieved"
    req_res_log }o--o| semantic_cache : "cache_hit = answered from an entry"
```

Indexes:
- **`req_res_log`:** a unique index on `(tenant_id, idempotency_key)`, which is the idempotency lock, plus `(created_at)`, `(source, created_at)`, `(provider, model_used)` and `(conversation_id, created_at)`.
- **`ingest_log`:** `(run_id)` and `(doc_id, created_at)`.
- **`semantic_cache`:** `(tenant_id, prompt_version, corpus_hash, embedding_model)`.

There are no foreign keys between the tables on purpose. A log row must be writable even if the related row is gone or was never written (best-effort telemetry).

### 10.4 Which step touches which store

| Step | `req_res_log` | `semantic_cache` | `ingest_log` | Chroma | If the store is down |
|---|---|---|---|---|---|
| idempotency claim / replay | INSERT/UPDATE · READ | | | | proceed without replay protection (logged) |
| conversation memory | READ | | | | answer without history |
| scope guard | | | | READ (bge, top-3) | fail open: in scope |
| semantic cache | | READ, then UPDATE hit_count | | READ (re-check) | treat as a miss |
| retrieval | | | | READ (one collection per model) + BM25 in memory | BM25 only / static mode |
| store the answer | | INSERT | | | answer anyway |
| end of call | UPSERT (full row) | | | | logged `req_res_log_write_failed` |
| ingest (script / admin) | | DELETE stale rows | INSERT per doc per model | WRITE | doc marked `failed`; re-run retries it |

### 10.5 API catalogue (`/api/v1`)

| Method | Path | Key | Idempotency-Key | Rate limit | Writes | Main errors |
|---|---|---|---|---|---|---|
| GET | `/health` | none | – | – | – | – |
| GET | `/ready` | none | – | – | – | 503 if Postgres / Chroma / corpus / prompt missing |
| POST | `/chat` | user | **required** | 60/min | req_res_log, semantic_cache | 400 · 401 · 404 CUSTOMER_NOT_FOUND · 409 · 422 · 429 · 504 |
| POST | `/chat/stream` | user | **required** | 60/min | same (final answer stored for replay) | same; SSE `error` event |
| POST | `/retrieval/search` | user | **required** | 60/min | req_res_log | 400 · 401 · 429 |
| GET | `/requests`, `/requests/summary`, `/requests/{id}` | user | – | – | – | 401 · 404 |
| GET | `/metrics` (JSON / `Accept: text/plain`) | user | – | – | – | 401 |
| GET | `/prompts`, `/prompts/active` | user | – | – | – | 401 |
| POST | `/admin/ingest[?force=true]` | admin | **required** | – | Chroma, ingest_log, semantic_cache (stale delete) | 401 · 403 |
| GET | `/admin/ingest/runs[/{run_id}]` | admin | – | – | – | 401 · 403 · 404 |
| GET / DELETE | `/admin/cache` | admin | – | – | DELETE clears semantic_cache | 401 · 403 |
| GET | `/admin/vector-store[?doc_id=]` | admin | – | – | – | 401 · 403 |
| POST | `/eval/run` | admin | **required** | – | eval/results files, req_res_log (source eval) | 401 · 403 (no 30 s timeout) |
| GET | `/eval/runs[/{run_id}]` | user | – | – | – | 401 · 404 |

**Designed outcomes are 200, not errors:** `declined`, `refused`, `answered_with_gap`, `degraded`, `out_of_scope`. Only an unknown customer is a 404.

**Error envelope:** `{"error": {"code", "message", "request_id"}}`. `request_id` equals the Idempotency-Key, so it matches the log file and `req_res_log`.
