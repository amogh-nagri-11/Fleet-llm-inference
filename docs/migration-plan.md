# Fleet v2 — Phase 1 Migration Plan

Produced per `REDESIGN.md` §85 before any Phase 2+ implementation. Format per
component: what it does today, keep/modify/replace, and why.

## Gateway

| Existing component | What it does | Decision | Reason |
|---|---|---|---|
| `gateway/main.py` | FastAPI app assembly, lifespan (starts load balancer, worker pool, autoscaler, health checker) | **KEEP** | Correct shape. Phase 2+ will extend the lifespan to also start the context/memory layer, not replace it. |
| `gateway/routes.py` | `/health`, `/generate`, `/chat`, `/workers`, `/queued/generate`, `/queue/depth`, all under `/api/v1` | **MODIFY** | Phase 2 adds `agent_id`/`workflow_id`/`request_id`/`parent_request_id` to request models. Phase 9 inserts the context engine between request validation and `load_balancer.pick_worker()`. Fixed now (Phase 1): `pick_worker()` was called outside the try/except in `/generate` and `/chat`, so a tripped circuit breaker produced an unhandled 500 instead of a clean 503 — same bug already fixed in `worker_pool.py`. |
| `gateway/middleware.py` | Prometheus request-metrics middleware | **KEEP** (cleaned up) | `prometheus_middleware` and `metrics_endpoint` were dead code — never imported anywhere, `main.py` uses `metrics_middleware` and defines its own `/metrics` route. Removed as part of Phase 1 "clean architecture." |
| `gateway/metrics.py` | All Prometheus metric definitions (single source of truth) | **KEEP + EXTEND** | Phase 13 adds context/agent/memory metrics (`fleet_context_tokens_*`, `fleet_agent_*`) here, following the existing pattern — no redefinition elsewhere per `CLAUDE.md` conventions. |

## Config

| Existing component | What it does | Decision | Reason |
|---|---|---|---|
| `config/settings.py` | Plain `os.getenv` config, no pydantic-settings | **KEEP + EXTEND** | Phase 4/6 add `CONTEXT_BUDGET_DEFAULT`, `MEMORY_DB_URL`, etc. following the existing style. Fixed now (Phase 1): `REQUEST_TIMEOUT` was read into settings but never actually passed to `OllamaClient` — `load_balancer.py` constructed clients with the hardcoded default (120s) instead. Now wired through. |

## Inference layer (retained from the earlier scheduling-first draft, per §0.1)

| Existing component | What it does | Decision | Reason |
|---|---|---|---|
| `router/load_balancer.py` | Worker selection (round_robin / least_latency / queue_depth), runtime add/remove worker | **KEEP**, extend later | Phase 9 adds `context_aware`/`token_aware`/`slo_aware` as additional strategies alongside the existing three — additive, not a rewrite. |
| `router/circuit_breaker.py` | Per-worker CLOSED/OPEN/HALF_OPEN state machine | **KEEP** | Already correct and covered by new tests (`tests/test_circuit_breaker.py`). No redesign need touches this. |
| `router/health_checker.py` | 15s background health-check loop | **KEEP** | Sufficient for v2. Full heartbeat protocol (GPU util, tokens/sec) from the earlier draft is not part of this doc's scope. |
| `router/autoscaler.py` | Docker spin-up / static-registration fallback, queue-depth-based scale up/down | **KEEP**, extend later | Phase 9+/future work: scale decisions should factor in context/token demand (§46), not just queue depth. Not a Phase 1 change. |
| `workers/ollama_client.py` | Wraps Ollama `/api/chat`, tracks per-worker stats | **KEEP** | No functional issues found. Benefits from the `REQUEST_TIMEOUT` fix above. |
| `workers/worker_pool.py` | Redis `RPUSH`/`BLPOP` queue consumer | **REPLACE in Phase 10** | Per REDESIGN.md §43/§0.2, this becomes Redis Streams with consumer groups, ACK, retry, and a dead-letter queue. Already fixed (prior session): `pick_worker()` failures were silently dropped instead of writing an error result — now covered by `tests/test_worker_pool.py`. |
| Redis list queue (`llm:request_queue`) | Simple FIFO via `RPUSH`/`BLPOP` | **REPLACE in Phase 10** | Same as above — no reliable delivery, no recovery on worker crash. |

## New in v2 (no existing counterpart)

`context/`, `memory/`, `workflows/` packages (per REDESIGN.md §71) do not exist yet.
Decision: **do not pre-create the full target tree now.** Each new top-level
package is added in the phase that first needs it (`context/` in Phase 3,
`memory/` in Phase 6, `workflows/` in Phase 2) — creating empty scaffolding
ahead of time invites drift between the plan and what's actually implemented.

## Deferred (per REDESIGN.md §0.2 — not touched in this migration)

* `workers/vllm.py` — interface stub only, no implementation.
* Semantic memory / vector search.
* Full `FleetAgent` SDK (thin client only, §64).
* Cost-based budgets.

## Phase 1 stabilization changes made

1. `gateway/routes.py` — `pick_worker()` moved inside try/except for `/generate` and `/chat` (503 instead of unhandled 500 when no worker is available).
2. `router/load_balancer.py` — `REQUEST_TIMEOUT` setting now actually passed to `OllamaClient` (was silently ignored).
3. `gateway/middleware.py` — removed dead `prometheus_middleware`/`metrics_endpoint`.
4. `pytest.ini` + `gateway/requirements.txt` — added `pytest`/`pytest-asyncio`; project had zero test coverage before this.
5. `tests/` — 29 unit tests added: circuit breaker state machine, load balancer strategies + worker registration, gateway routes (auth, success/failure paths, the 503-not-500 fix), and a regression test for the `worker_pool.py` silent-drop bug.

Existing functionality was not removed. `docker-compose`/k8s manifests, autoscaler
Docker-vs-static fallback, and all routing strategies are unchanged in behavior.

**Note:** the same `pick_worker()`-outside-try-except pattern this fixes in
`gateway/routes.py` was already fixed in `workers/worker_pool.py` on `main`
(commit `c86216a`, prior to branching). Not backported further — `main` and
this branch now agree on the queue-side fix; the route-side fix is new here.

## Phase 2 — request/workflow metadata

Scope kept to identity fields only, per REDESIGN.md §72 Phase 2 (not the
fuller §6 example schema — `context_budget`, `priority`, SLO fields, and
`memory{}` belong to their own later phases).

* `gateway/routes.py` — `agent_id`, `workflow_id`, `request_id`,
  `parent_request_id` added as an `AgentMetadata` base model shared by
  `GenerateRequest`/`ChatRequest`. All optional. `request_id` is
  server-generated when the caller omits it, so every request is traceable
  regardless of whether the caller is agent-aware. Echoed back in the
  response (only non-null fields, to keep the response shape stable for
  existing callers). Logged at receipt (`event=received`) per §36's log
  correlation guidance — full tracing infra is still Phase 13.
* `workers/worker_pool.py` — the three metadata fields ride along in the
  Redis queue payload for `/queued/generate`, but are explicitly popped
  before `**job` is spread into `worker.generate()`/`worker.chat()` (those
  methods don't accept them — this would have been a `TypeError` on every
  queued request otherwise). Covered by
  `test_agent_metadata_is_stripped_before_dispatch_to_worker`.
* **Deliberately not built yet:** persistent workflow state (§37 — status,
  step, last_error). That needs a durable store, which arrives with
  PostgreSQL in Phase 6. Phase 2 is pure request/response identity plumbing.
* 5 new tests added (34 total): metadata round-trip on `/generate`/`/chat`,
  auto-generated vs. caller-supplied `request_id`, and the queue-side
  stripping regression guard above.

## Phase 3 — context abstraction

New `context/` package, per REDESIGN.md §7/§72 Phase 3. Not wired into the
live request path yet — that integration is explicitly Phase 9 ("connect
the context engine to the existing worker scheduler"). Building it in now
would mean half-wiring a feature before ranking (Phase 4), memory (Phase
6/7), and compression (Phase 8) exist to feed it, so `gateway/routes.py` is
untouched this phase.

* `context/models.py` — `ContextItem` dataclass (id, type, content,
  token_count, created_at, last_accessed_at, importance, relevance, source,
  agent_id, workflow_id) and `ContextType` enum, exactly the fields/types
  listed in §7. `estimate_tokens()` is a rough ~4-chars/token heuristic,
  explicitly documented as not a real tokenizer — Phase 4 owns actual
  budgeting math and can swap it without changing `ContextItem`'s shape.
* `context/store.py` — `ContextStore`, in-memory only (no Postgres until
  Phase 6), scoped by `workflow_id`. `add`/`get`/`list_for_workflow`
  (optionally filtered by type)/`delete`/`clear_workflow`/`total_tokens`.
* `context/manager.py` — `ContextManager.record()` creates+stores an item;
  `get_candidate_context(workflow_id)` returns the **unranked, unbudgeted**
  pool for a workflow — deliberately not doing selection/ranking/packing
  yet, that's Phase 4. Module-level `context_manager` singleton, matching
  the existing pattern (`load_balancer`, `worker_pool`, `autoscaler`).
* **Deliberately not built yet:** full context lifecycle state machine
  (§36 — ARCHIVED/EXPIRED), ranking/selection (Phase 4), compression (Phase
  8). `ContextItem` only tracks `created_at`/`last_accessed_at` for now.
* 14 new tests added (48 total): token estimation, item creation/touch,
  store CRUD + workflow scoping/isolation + type filtering, manager
  record/retrieve/isolation.

## Phase 4 — context budgeting

New `context/selection.py`, per REDESIGN.md §9-§12/§72 Phase 4. Still not
wired into `gateway/routes.py` — same reasoning as Phase 3, integration is
Phase 9.

* Five policies exactly per §11: `full` (chronological baseline — what a
  naive system does, just append history until it doesn't fit), `recent`,
  `relevance`, `budget_aware` (greedy pack by `importance/token_count`
  density), `hybrid` (recommended — greedy pack by weighted
  relevance+recency+importance score / token_count, §10's formula).
* §10's formula also has a `workflow_weight * workflow_match` term — omitted
  and documented in-code: every candidate already comes pre-scoped to one
  workflow via `ContextStore.list_for_workflow`, so that term is a constant
  1.0 until Phase 7 (memory retrieval) introduces cross-workflow candidates.
* Packing is greedy (first-fit-decreasing by the policy's key), per §12's
  explicit instruction not to build a full knapsack solver "unless
  benchmarking shows it is necessary" — it doesn't, yet.
* `ContextManager.get_budgeted_context(workflow_id, budget_tokens, policy)`
  composes `get_candidate_context()` + `select_context()`.
* `config/settings.py` / `.env.example` — added `CONTEXT_BUDGET_DEFAULT`
  (8192) and `CONTEXT_SELECTION_POLICY` (hybrid), unused until Phase 9 wires
  them in, but defined now alongside the concept they configure.
* **Benchmarked** (§72's explicit instruction for this phase) via
  `scripts/benchmark_context_selection.py` — a synthetic coding-agent
  workflow (30 steps, mostly small conversation/tool-result items, a few
  large tool dumps, occasional high-importance errors), 4000-token budget:

  ```
  Policy          Selected Tok   Saved Tok     Items  Avg Importance
  ------------------------------------------------------------------
  full                    3996        4700     16/31           0.410
  recent                  4000        4696     11/31           0.457
  relevance               3996        4700     25/31           0.457
  budget_aware            3696        5000     27/31           0.427
  hybrid                  3696        5000     27/31           0.427
  ```

  This is a mechanics check (does packing respect the budget, does it favor
  higher-value items, how many tokens does each policy save), not the §53
  "Full History vs Budgeted Context" experiment — that needs a live model
  and task-success measurement against real agent workloads, which is
  Phase 14. Numbers above are real output from the script, not invented
  (§39/§60), reproducible with `--seed 42` (the default).
* **Deliberately not built yet:** compression (Phase 8), memory-sourced
  candidates (Phase 6/7), a real tokenizer (still the Phase 3 ~4-char
  heuristic — swapping it doesn't change any policy's logic).
* 16 new tests added (64 total): every policy respects budget, unknown
  policy raises, empty input, budget larger than everything, policy-specific
  ordering (recent/full/relevance/budget_aware each verified to prefer what
  they claim to), budget_aware packing multiple small high-value items over
  one big low-value one, hybrid tie-breaking, `ContextManager` integration.

## Phase 5 — artifact handling

New `context/artifacts.py`, per REDESIGN.md §32-35/§72 Phase 5. Still not
wired into `gateway/routes.py` — same reasoning as Phases 3/4, integration
is Phase 9.

* `ArtifactType` enum matches §34's list literally: `file`, `tool_output`,
  `log`, `document`. Distinct from `ContextType` on purpose — `ContextType`
  describes a context item's *role*, `ArtifactType` describes what kind of
  large content is being externalized.
* `summarize_text()` — deterministic, rule-based (keyword-match lines like
  "error"/"fail"/"exception"/"traceback", falling back to first-N-lines if
  nothing matches). No LLM call. Mirrors §33's worked example directly
  ("Relevant failures: ..."). Real semantic summarization is explicitly
  Phase 8's job (§13 — routes through the inference layer); Phase 5 only
  needs something small and useful instead of the raw dump.
* `Artifact` — full content + auto-computed summary/token_count.
  `ArtifactStore.create/get/get_excerpt` (line-range retrieval, §35's
  `relevant_ranges`) `/to_reference_item` (builds the small `ContextItem`
  that actually goes into context — `type=SUMMARY`, `content=summary`, not
  the raw content).
* `context/models.py` — added `ContextItem.artifact_id: Optional[str]`, the
  natural link back to the full artifact (§35's reference JSON shape).
  Small, additive change to a Phase 3 model, not a redesign of it.
* `ContextManager.record_artifact()` — creates the artifact + stores only
  the reference item, in one call. `get_artifact()`/`get_artifact_excerpt()`
  close the retrieval loop from §33 ("the agent can request the full output
  when necessary").
* **Deliberately not built yet:** automatic large-vs-small routing (no
  token-count threshold that decides `record()` vs `record_artifact()` for
  you) — callers choose explicitly for now, keeping behavior predictable
  rather than adding an implicit heuristic nobody asked for yet.
* 14 new tests added (78 total): summarizer keyword-matching/fallback/
  truncation-notice behavior, artifact creation, store CRUD + excerpt
  retrieval, reference-item size (asserted at least 10x smaller than the
  artifact it points to), full `ContextManager` round-trip (record → small
  reference in context → fetch full content/excerpt back by id).

## Phase 6 — memory (working + episodic only)

New `memory/` package, per REDESIGN.md §14-§21/§72 Phase 6. Scope is
working + episodic memory only — semantic memory is deferred (§0.2).

* **New infrastructure dependency: PostgreSQL.** Not available as a
  container here (`CLAUDE.md`'s own gotcha — no Docker in this WSL
  environment), so installed natively (`apt-get install postgresql`), same
  tier as Redis/Ollama in this setup. Asked the user how to handle this
  before writing any code, since introducing a new external dependency
  isn't a call to make unilaterally; they chose native install + peer auth
  (a `fleet` OS-user-matching role, unix-socket connection, no password) —
  the config stays generic enough (`MEMORY_DB_HOST` accepts either a
  hostname for TCP or a socket directory for peer auth) that a real
  password-based TCP role works too without code changes.
* `memory/models.py` — `MemoryKind` (`working`/`episodic` only — see §0.2),
  `MemoryItem` (id, kind, content, agent_id, workflow_id, importance,
  created_at, last_used_at, access_count, expires_at). No `relevance`
  field, unlike `ContextItem`: relevance is judged against a specific
  request at retrieval time (Phase 7), not intrinsic to the memory — §20's
  own metadata list doesn't include it either.
* `memory/store.py` — `MemoryStore`, asyncpg-backed. `ensure_schema()` runs
  idempotent `CREATE TABLE IF NOT EXISTS` (no migration framework yet,
  matching §18's "start with simple storage"). `get()` updates
  `last_used_at`/`access_count` on read (§20's access tracking).
  `purge_expired()` removes working memory past `expires_at` (§15) —
  episodic memory has no `expires_at` and is never touched by it.
* `memory/manager.py` — `MemoryManager.record_working()` (accepts
  `ttl_seconds`, sets `expires_at`) / `record_episodic()` (never expires) /
  `list_for_workflow()`. Async throughout, unlike `ContextManager` — this
  is genuinely doing I/O now, not in-memory bookkeeping.
* `config/settings.py` / `.env.example` — `MEMORY_DB_HOST/PORT/NAME/USER/
  PASSWORD`. Generic defaults (`fleet`/`localhost`), not the actual local
  peer-auth username — that only lives in the gitignored local `.env`.
* `docker-compose.yml` — added a `postgres` service (image
  `postgres:16-alpine`, health-checked, same pattern as `redis`) so the
  Docker path stays consistent with what local dev now needs. Untested
  here (no Docker in this WSL environment) — config-only, mirrors the
  `redis` service's established shape exactly.
* **Deliberately not built yet:** memory retrieval/ranking/injection into
  the context pipeline (§19 — that's Phase 7), decay scoring (§21 — also
  Phase 7, it's a retrieval-time ranking concern). Migrating `ContextStore`/
  `ArtifactStore` (Phase 3/5) to Postgres — REDESIGN.md §72's Phase 6 text
  says only "working memory, episodic memory," not "make everything
  durable"; those stay in-memory. A `workflows`/`agents` table (§18's
  architecture diagram lists them) — no phase in §72 actually asks for a
  workflow-state table; the note in the Phase 2 entry above overstated
  this as arriving "with Postgres in Phase 6" — correcting that here:
  workflow state (§37) remains unscheduled, not tied to a specific phase.
* 11 new tests added (89 total), run against the real local Postgres
  instance (not mocked — asyncpg against a fake is more effort than value
  here, and the real DB is now available): store CRUD, access tracking on
  `get()`, workflow/kind scoping, expiry purge (working memory only,
  episodic untouched), manager TTL handling. Each test uses a unique
  `test-<uuid>` workflow id and the fixture deletes all `test-%` rows on
  teardown, so the suite is safe to run repeatedly against the same DB.

## Phase 7 — memory retrieval

New `memory/ranking.py` + `memory/retrieval.py`, per REDESIGN.md
§19-21/§72 Phase 7: retrieve, rank, budget, inject.

* `memory/ranking.py` — `score_memories()` implements §21's
  `memory_score = relevance + importance + recency + access_frequency` as
  a weighted sum (equal weights by default — §21, unlike §10, doesn't
  commit to specific values). `recency` is normalized from
  `last_used_at` (not `created_at`) — decay is about staleness of *usage*.
  `access_frequency` is normalized from `access_count`. `relevance` uses a
  new `lexical_relevance()`: deterministic word-overlap (Jaccard
  similarity) between query and memory content — **not** semantic search.
  Real semantic relevance needs embeddings, which are explicitly deferred
  (§0.2); this is the same "deterministic and measurable first" approach
  §10 already established for context selection, applied here because
  memory retrieval needs *some* relevance signal and none was stored in
  Phase 6 (relevance is request-specific, not intrinsic to a memory).
  `rank_memories()` is a separate, independently-testable sort-by-score
  step, matching §19's named "rank" stage explicitly (not folded silently
  into budgeting).
* `memory/retrieval.py` — `select_within_budget()` greedy-packs by
  score/token-cost density (mirrors `context/selection.py`'s approach
  exactly, reusing `context.models.estimate_tokens()` rather than a second
  heuristic). `to_context_items()` is the "inject" step — converts
  retrieved `MemoryItem`s into `ContextItem`s (`type=MEMORY`, which has
  existed since Phase 3). `retrieve_relevant_memories()` composes rank +
  budget over an already-fetched pool; "retrieve" itself stays a DB call
  (`MemoryManager.list_for_workflow`), not something a pure function needs.
* `MemoryManager.get_relevant_memories()` / `get_relevant_context()` —
  the full retrieve→rank→budget(→inject) pipeline as manager methods.
  Still not wired into `gateway/routes.py` or `ContextStore` — that
  integration is Phase 9, same discipline as every context-producing
  phase so far (the "inject" step builds `ContextItem`s but doesn't call
  `context_manager.store.add()` itself).
* 22 new tests added (111 total): ranking (relevance/importance/recency/
  access_frequency each isolated via zeroed weights to prove they
  independently affect the score, plus a single-item no-divide-by-zero
  case), retrieval (budget respected, density preference, empty input,
  context-item conversion), and `MemoryManager` integration tests against
  the real DB (query-relevant memory beats an irrelevant one at equal
  importance, budget respected, kind filtering, empty workflow).

## Phase 8 — context compression

New `context/compression.py`, per REDESIGN.md §13/§72 Phase 8: "old
conversation -> summary -> compressed context." This is the **first phase
that actually calls the inference layer** — every prior context/memory
phase (3-7) was deliberately deterministic/no-LLM. §13 explicitly wants
this: "the summarization backend can use the same inference infrastructure,"
describing a recursive workflow (Fleet requests summarization, which is
itself an inference request through the existing worker layer).

* `Summarizer` is an injected async callable (`str -> str`), not a
  hardcoded live call — `compress_items()` and `ContextManager.
  compress_old_context()` stay unit-testable without a running model, same
  as every prior phase. `llm_summarizer()` is the one real implementation,
  passed in explicitly by whoever calls compression in production.
* `llm_summarizer()` routes through `router.load_balancer` — the same
  `pick_worker()`/`generate()`/`record_success`/`record_failure` path as
  `gateway/routes.py`, including the exact **same fixed pattern from Phase
  1**: `pick_worker()` sits outside the try/except, so a "no workers
  available" error isn't wrongly blamed on a worker that was never picked.
  Regression-guarded with the same style of test used for that original fix.
* This is the first import from `context/` into `router/` — checked for
  cycles (`router`/`workers`/`config` import nothing from `context`) before
  wiring it in; none exist.
* `CompressionResult` — `tokens_before`/`tokens_after`/`tokens_saved`,
  satisfying §13's explicit "this should be measurable" instruction.
* `ContextManager.compress_old_context(workflow_id, summarizer, max_items,
  context_type)` — takes the oldest items (optionally one type only),
  replaces them in the store with a single `SUMMARY` item. Explicit,
  caller-invoked — not triggered automatically when a workflow goes over
  budget, same "no implicit heuristic nobody asked for yet" stance as
  Phase 5's `record()` vs `record_artifact()`. Still not wired into
  `gateway/routes.py` — that's Phase 9.
* **Not tested against a live model.** Ollama is currently not running in
  this environment (connection refused on :11434 — same underlying
  install issue flagged earlier in this session, still unresolved). All
  compression tests use `fake_summarizer`/mocked workers, consistent with
  how the rest of the suite avoids depending on a live Ollama instance.
  `llm_summarizer()` itself is real, tested end-to-end down to the mocked
  worker boundary — only the actual model call is unverified live.
* 14 new tests added (125 total): pure `compress_items()` behavior
  (summary type/source, workflow/agent_id inheritance, importance = max of
  originals, empty-list rejection, real token-savings measurement),
  `llm_summarizer()` against a mocked worker (prompt/model passed through,
  success/failure paths, the no-worker-available regression guard), and
  `ContextManager.compress_old_context()` integration (store mutation,
  `max_items`, type filtering, empty-workflow no-op).

## Phase 9 — context-aware inference routing

Per REDESIGN.md §72 Phase 9's one line: "Connect the context engine to the
existing worker scheduler." This is the **first phase that touches
`gateway/routes.py`'s live request path** — every context/memory phase
before this (3-8) deliberately stayed unwired. Ollama was fixed and
verified live earlier this session, so this phase is verified against a
real running gateway + real model, not just mocks.

* **Scoped narrowly to what §72 actually asks**, not the fuller §24/§40/§41
  architecture (token-aware/SLO-aware scheduling, a worker capability
  registry, streaming-aware TTFT routing) — those aren't in any Phase 1-15
  checklist item. What shipped: workers gained a hard context-capacity
  limit (`OllamaClient.max_context_tokens`, default 8192 from
  `WORKER_MAX_CONTEXT_TOKENS`, matching llama3:8b's real context window),
  and `LoadBalancer.pick_worker(context_tokens=...)` excludes workers that
  can't hold that much context — per §41's example exactly: a merely-busy
  worker stays eligible (filtering is on hard capacity, not load); a
  worker with insufficient capacity doesn't, regardless of `ROUTING_STRATEGY`.
* **Opt-in, not a behavior change for existing callers.** `gateway/routes.py`
  only records context and computes `context_tokens` when the caller
  supplies `workflow_id` (Phase 2's optional metadata). No `workflow_id` →
  `pick_worker(context_tokens=None)` → identical routing to every phase
  before this one. Verified with a dedicated test
  (`test_no_context_tokens_arg_behaves_identically_to_before_phase_9`).
* `/generate`/`/chat` record the request content into `context_manager`
  (Phase 3, `type=CONVERSATION`) and use the workflow's running
  `total_tokens()` as the routing signal — not just this request's size,
  matching §41's framing of context *accumulating* over a session.
  `/queued/generate` carries `context_tokens` through the Redis payload the
  same way Phase 2 carries `agent_id`/`workflow_id` — computed at enqueue
  time, popped in `worker_pool.py` before `**job` dispatch (same
  metadata-stripping pattern as before), passed to `pick_worker()`.
* This is the first time `context/` and the live gateway meet — verified
  live: two real `/generate` calls in the same `workflow_id` against the
  actual running gateway + Ollama, confirmed via the `[Fleet] event=received`
  log that both were recorded, and `scripts/smoke_test.py` passing 9/9
  against real inference (not mocks) after the change.
* Existing tests needed a small mechanical update: every test that
  monkeypatched `load_balancer.pick_worker` with a zero-arg lambda now
  takes `**kwargs` (the real signature gained an optional
  `context_tokens` parameter). No behavioral test changes, just signature
  compatibility.
* **Deliberately not built yet:** per-worker heterogeneous capacity
  tracking (every worker uses the same configured limit — this codebase
  has no worker-capability registry, unlike the superseded draft),
  token/SLO-aware scoring (§40's other named policies), memory injection
  into the actual prompt sent to the model (Phase 7's retrieval pipeline
  exists and is tested, but nothing calls it from `gateway/routes.py`
  yet — no phase explicitly assigns that wiring; noted as a gap, not a
  broken promise).
* 14 new tests added (139 total): load-balancer capacity
  filtering (exclusion, specific error message, health/circuit
  interaction, "busy but eligible" per §41), and live-wired route tests
  using the real `context_manager` singleton (not mocked — the point is
  proving it's actually connected) with unique per-test workflow ids:
  context_tokens present only when `workflow_id` is given, growing across
  requests in the same workflow, `/queued/generate` payload forwarding,
  and a `worker_pool.py` regression guard that `context_tokens` doesn't
  leak into `**job`.

## Phase 10 — reliable queue (Redis Streams)

Per REDESIGN.md §43/§72 Phase 10: "Upgrade the existing queue to Redis
Streams with: ACK, retry, recovery, dead-letter queue." Replaces the
`RPUSH`/`BLPOP` queue `workers/worker_pool.py` used through Phase 9 —
flagged as a **REPLACE** item since the Phase 1 migration plan.

* **New package named `job_queue/`, not the REDESIGN.md §71 literal
  `queue/`.** Checked first: `queue` is a Python stdlib module
  (thread-safe queues, used internally by uvicorn/starlette/anyio), and
  since the repo root has to stay on `sys.path` for `gateway.main` etc. to
  import, a top-level `queue/` package would shadow it for every
  dependency doing `import queue` — confirmed the collision
  (`import queue; queue.__file__` resolved to our package) before
  choosing `job_queue/` instead. Noted here because it's the one place
  this document's literal file naming can't be followed as written.
* `job_queue/streams.py` — `RedisStreamQueue`: `XADD`/`XREADGROUP`/`XACK`
  wrapper, consumer group created idempotently (`XGROUP CREATE` with
  `BUSYGROUP` treated as success, not an error).
* `job_queue/retry.py` — `RetryPolicy.recover()`: `XAUTOCLAIM`s pending
  entries idle past `QUEUE_PENDING_MIN_IDLE_MS`, splits them into
  retryable vs. dead-lettered using **Redis' own per-entry delivery
  counter** (`XPENDING`'s `times_delivered`) rather than in-process state
  — that counter survives process restarts and is shared across every
  consumer, which an in-memory dict on a single `WorkerPool` instance
  wouldn't be.
* `job_queue/dead_letter.py` — `DeadLetterQueue`: a separate stream, so
  exhausted jobs stay inspectable (`list_all()`) instead of silently
  vanishing.
* `workers/worker_pool.py` rewritten internally but **public interface
  unchanged** (`enqueue`/`get_result`/`queue_depth`/`connect`/`start`/
  `stop`) — `gateway/routes.py` needed zero changes. Dispatch logic
  factored into `_process_job()`, shared by both the normal consume loop
  and a new `_recovery_loop()` that runs `RetryPolicy.recover()` every
  `QUEUE_RECOVERY_INTERVAL_SECONDS` and reprocesses whatever comes back
  retryable.
* **Idempotency (§18)** comes for free from the existing design, not new
  machinery: results are already keyed by `request_id`
  (`llm:result:<id>`), so a reclaimed job being reprocessed just
  overwrites the same key with an equivalent value. No dedup logic added.
* **Queue key changed**: `llm:stream:request_queue` (Stream), not the old
  `llm:request_queue` (List) — a Stream is a different Redis type, so
  reusing the key would `WRONGTYPE` against any leftover data even before
  considering that it's semantically a different structure now.
  `CLAUDE.md`'s documented convention updated to match.
* **Deliberately not built yet**: event-based result delivery. §44
  explicitly flags polling as wasteful and suggests pub/sub, but that's
  not itemized in §72 Phase 10's checklist (ACK/retry/recovery/dead-letter
  only) — `get_result()` still polls `llm:result:<id>`, unchanged,
  documented in-code as a known, deliberately deferred gap.
* Verified live: real Redis Streams state inspected directly after running
  `scripts/smoke_test.py` against the running gateway — `XINFO GROUPS`
  shows the `fleet-workers` consumer group with 1 entry processed, `XPENDING`
  shows 0 pending (fully acked), confirming the whole path end-to-end
  through a real model call, not just the test suite.
* 15 new tests added (154 total): `job_queue/` tested against real local
  Redis (same philosophy as Postgres in Phase 6 — mocking `XAUTOCLAIM`/
  `XPENDING` faithfully would be more work than using the real thing),
  including competing consumers not receiving the same message, dead-letter
  after exceeding retries, and idle-time gating (an actively-processing
  job isn't reclaimed out from under its consumer). `tests/test_worker_pool.py`
  rewritten: the old `FakeRedis` mock modeled the List-based queue
  interface, which no longer exists, so regression guards
  (no-worker/metadata-stripping/context_tokens) now test `_process_job()`
  directly — pure, no Redis needed — and loop-level behavior (ack, crash
  recovery, dead-lettering) runs against real Redis with unique per-test
  stream keys.

## Phase 11 — reference coding agent

Per REDESIGN.md §28-31/§64/§72 Phase 11. Deliberately simple (§28) — not
an agent framework, a demonstration that the Fleet loop from §31 actually
works against a real running gateway and a real model.

* `client/fleet_client.py` — `FleetClient`, the thin reference SDK §64
  explicitly keeps in scope (only the higher-level `FleetAgent` wrapper,
  §65, is deferred per §0.2). No retry/backoff/streaming logic — a
  convenience wrapper over `httpx`, nothing more. Tested with
  `httpx.MockTransport` (request shape/auth header/error propagation), no
  live server needed for those 7 tests.
* `examples/coding_agent/tools.py` — the four hardcoded tools from §28
  (`read_file`, `write_file`, `search_code`, `run_tests`), **not
  mocked** — real file I/O and a real `pytest` subprocess run, scoped to
  a small sandbox directory with explicit path-escape rejection (the
  agent operates on real files, but never outside its own sandbox).
  `run_tests()` uses `sys.executable`, not a bare `python3` — otherwise it
  silently runs against the system interpreter, which doesn't have pytest
  installed (caught by the test suite, not assumed).
* `examples/coding_agent/sandbox_repo/` — a tiny toy project with one
  intentional bug (`add()` returns `a - b`) and a test that catches it,
  matching §62's "repository with intentional bug" example.
* `examples/coding_agent/agent.py` — the reference agent. Six-step fixed
  sequence, not a planner: three steps are **real inference calls through
  Fleet** (understand the task, analyze the bug, summarize the outcome —
  all via `FleetClient.chat()` with `agent_id`/`workflow_id` set, so they
  exercise Phase 9's context-aware routing for real), three are real tool
  calls (`read_file`, `write_file`, `run_tests`). The code fix itself is
  scripted (a known string replacement), not parsed from the model's
  freeform response — explicitly documented in the module docstring as a
  deliberate scope decision: reliably extracting an exact patch from an
  8B model's prose is a hard problem unrelated to what this demo is
  for (Fleet's infrastructure, not code-fixing intelligence).
* `pytest.ini` — added `testpaths = tests`. Without it, a bare `pytest`
  run from the repo root would recursively discover
  `sandbox_repo/test_calculator.py` and run it as if it were part of
  Fleet's real test suite — it isn't; it's a deliberately-buggy demo
  fixture.
* **Verified live, fully**: ran `examples/coding_agent/agent.py` against
  the real running gateway + real Ollama. The model correctly diagnosed
  the bug in its own words ("The `add` function is supposed to add two
  numbers together but instead it subtracts them"), the scripted fix was
  applied, `run_tests()` went from FAILED to PASSED for real, and the
  gateway log confirms all three chat calls shared one `workflow_id`
  under `agent_id=reference-coding-agent` — Phase 9's wiring exercised by
  a real caller, not just tests. Sandbox reset to its buggy state
  afterward so the repo stays reproducible for the next run.
* 15 new tests added (169 total): `FleetClient` request-shape/auth/error
  tests via `httpx.MockTransport`, and `tools.py` tests including running
  real pytest against the intentionally-buggy sandbox (asserts it
  actually fails) and against a scripted fix (asserts it actually
  passes) — a fixture restores `calculator.py`'s original content after
  any test that writes to it.

## Phase 12 — agent workload simulator

Per REDESIGN.md §7-8/§72 Phase 12. Every simulated request goes through
`client/fleet_client.py` — the same real HTTP API a real caller uses
(§8: "do not create a fake benchmark path that bypasses Fleet"). Not
REDESIGN.md §37's benchmark/experiment harness (comparing scheduling
policies, measuring task success) — that's Phase 14. This is the traffic
generator those experiments would sit on top of.

* `scripts/simulate.py` — four workload profiles exactly per §7:
  `coding` (bursts of 3-6 small requests, short gaps — an agent
  interleaving LLM calls with tool calls), `research` (2-3 larger
  requests, longer gaps — pausing to "search"), `batch` (5-10 small
  requests, near-zero gaps), `mixed` (each simulated agent randomly gets
  one profile). Each simulated agent keeps one `agent_id`/`workflow_id`
  and loops through its workload pattern for the full `--duration`, not
  just once — matching §42's example CLI (`--duration 300`, sustained
  load, not a one-shot burst).
  `--arrival-rate` staggers agent start times instead of launching all of
  them at once, for a more realistic ramp-up at scale (§42's
  `--arrival-rate 20` example).
* **Reports only what's actually measurable, not §42's example fields
  verbatim**: latency here is full request round-trip, not TTFT (no
  streaming yet — that's a documented gap, not itemized in any phase
  1-15 checklist item), and there's no SLO-violation count (§39
  priorities/SLOs were never implemented — no phase 1-15 checklist item
  covers them either). The report explicitly says so rather than
  fabricating those fields to match the doc's example output shape.
* Verified live: ran `--agents 3 --workload mixed --duration 25` against
  the real gateway + Ollama. All 3 simulated agents happened to draw
  `batch` (small sample, `random.choice` over 3 options — not a bug), 5
  real requests completed with 0 failures; gateway log confirms three
  distinct `agent_id`/`workflow_id` pairs (`batch-agent-0/1/2`) running
  concurrently. Throughput (~0.12 req/s) and latency (P50 ~19s) reflect
  the real architecture accurately — one Ollama worker processes
  requests serially, so concurrent simulated agents queue up behind it.
  Real numbers from a real run, not invented (§39/§60's standing rule).
* 13 new tests added (182 total): `percentile()`/`make_prompt()` bounds,
  `assign_workloads()` (single-type vs. mixed distribution), and
  `run_agent()` against a `FakeClient` stub (no network) — verifies
  consistent `agent_id`/`workflow_id` across a whole agent's run,
  failure results recorded without raising, and `start_delay` (the
  `--arrival-rate` mechanism) is actually honored.

## Post-Phase-9 fix — rejected requests were permanently poisoning workflows

Found via adversarial live testing after Phase 12 (not by the unit suite —
every existing test mocked `pick_worker`, so none of them exercised a real
capacity rejection followed by a real follow-up request). Confirmed live,
end-to-end, before writing any fix:

1. `POST /generate` with `workflow_id=X`, a normal prompt → 200 (workflow
   established).
2. `POST /generate` with the same `workflow_id=X`, a ~10k-token prompt
   (over the 8192 worker limit) → correctly 503.
3. `POST /generate` with the same `workflow_id=X`, `prompt: "hi"` →
   **503, forever**, every time, until the gateway process restarts.

Root cause: `gateway/routes.py`'s `record_context_and_get_tokens()` called
`context_manager.record()` **before** `pick_worker()` had confirmed a
worker could actually take the request. `ContextStore` has no expiry
(nothing in Phases 3-9 added one), so the oversized rejected content sat
in the workflow's context forever, permanently pushing `total_tokens()`
above every worker's capacity. One bad prompt (or one large tool output,
once Phase 5's artifact path is ever live-wired) could permanently kill
an agent's entire session — silently, with an error message that reads
like a transient capacity problem, not "this workflow is now bricked."

Fix: split measuring from recording.

* `get_prospective_context_tokens(meta, content)` — pure read:
  `context_manager.total_tokens(workflow_id) + estimate_tokens(content)`.
  No side effect, safe to call before admission is decided.
* `record_context(meta, content)` — the actual write. Only called by
  `/generate` and `/chat` **after** `pick_worker()` returns successfully.
* `/queued/generate` never records at all — enqueuing and dispatch are
  separated in time (possibly by a lot, since jobs sit in a Redis Stream),
  so "can a worker handle this" can only be answered for real at actual
  dispatch time. Recording moved into `workers/worker_pool.py`'s
  `_process_job()`, in the `else` branch after `pick_worker()` succeeds
  there — using the job's own `workflow_id`/`agent_id`/prompt content,
  which was already flowing through the queue payload since Phase 2/9.
* This also makes the queued path more correct, not just fixed: capacity
  is now checked against the workflow's state *at actual dispatch time*,
  not a stale snapshot computed at enqueue time.
* Verified live: repeated the exact 3-request sequence above against a
  freshly restarted gateway. Step 3 now returns 200 with a real model
  response instead of a permanent 503.
* 5 new regression tests added (187 total): the exact 3-request sequence
  for both `/generate` and `/chat` (small → oversized-rejected → small
  must still succeed), `/queued/generate` provably not recording anything
  itself, and two `WorkerPool._process_job()`-level tests (rejected job
  records nothing, accepted job records exactly one `CONVERSATION` item).

## Post-Phase-9 fix — memory_manager not connected, WorkerPool.stop() leaking Redis

Two more findings from the same audit, both about lifecycle wiring rather
than request-handling correctness.

* **`memory_manager` was never connected.** `gateway/main.py`'s lifespan
  started/stopped `load_balancer`, `worker_pool`, `autoscaler`, and
  `health_checker`, but never called `memory_manager.store.connect()` —
  Phase 6/7's entire memory subsystem was fully built and tested but
  unreachable from the running app; `memory_manager.store.pool` was `None`
  in production. Fixed by adding `await memory_manager.store.connect(...)`
  to startup (same `MEMORY_DB_*` settings Phase 6 already defined) and
  `await memory_manager.store.close()` to shutdown. Verified live: killed
  and restarted the gateway, log shows `[MemoryManager] Connected to
  Postgres at /var/run/postgresql:5432`, and `\d memories` in `psql`
  confirms `ensure_schema()` actually ran against the real DB through
  this connection, not a test fixture. **Note:** this fixes the
  connection lifecycle only — no route calls `memory_manager` yet, so the
  retrieve/rank/budget pipeline from Phase 7 is still not wired into live
  request handling. That's a separate, larger piece of work, not
  attempted here.
* **`WorkerPool.stop()` didn't close its Redis connection.** Cancelled
  both loop tasks but left `self.redis` open — harmless for one
  long-lived process, but a real leak under repeated restarts. `stop()`
  is now `async`: cancels both tasks, awaits them to actually finish
  unwinding (suppressing `CancelledError`), then calls
  `self.redis.aclose()` and sets `self.redis = None`. `gateway/main.py`
  updated to `await worker_pool.stop()`. Verified live: sent a real
  `SIGTERM` (not `kill -9`) to a running gateway and confirmed a clean
  "Application shutdown complete" with no traceback.
* 6 new tests added (188 total): `WorkerPool.stop()` actually closes
  `self.redis` and marks both tasks done/cancelled (the `pool` test
  fixture also updated to tolerate a test having already closed the
  connection, rather than double-closing).

## Phase 13 — observability

Per REDESIGN.md §47-51/§72 Phase 13: "Add agent/workflow/context
metrics." Scoped honestly to what's actually live, not REDESIGN.md's
full metric wishlist — see the "deliberately not included" note below.

* **New metrics use the `fleet_` prefix**; the pre-existing metrics in
  `gateway/metrics.py` keep their `llm_` prefix rather than being
  renamed, so the existing Grafana dashboard's queries don't break.
* **Deliberately not labeled by `agent_id`/`workflow_id`.** Both are
  arbitrary, unbounded strings (every simulated/real workflow gets a
  fresh uuid) — an unbounded-cardinality Prometheus label is a
  well-documented way to take down a metrics backend. Per-agent/
  per-workflow detail stays available via the structured `[Fleet]
  event=received ...` logs already emitted since Phase 2; these metrics
  are fleet-wide aggregates.
* **Deliberately not added**: `fleet_context_tokens_before/after/saved`,
  `fleet_context_selection_seconds`, `fleet_context_compression_seconds`,
  `fleet_memory_retrieval_seconds`, `fleet_memory_hits/misses`. Those map
  to `context/selection.py`, `context/compression.py`, and
  `memory/retrieval.py` — none of which any live route calls (confirmed
  by the brutal live audit two sessions ago). A metric for code nothing
  invokes is a permanently-zero series that looks like a live signal and
  isn't one — worse than no metric. Also skipped: TTFT/TPOT (§50) —
  streaming isn't implemented, there's no token-by-token timing to
  measure; and `fleet_agent_workflow_cost`/`_duration` — cost tracking is
  explicitly deferred (§0.2) and no workflow-completion concept exists
  to measure duration against.
* What shipped instead, all tied to code that actually runs on every
  request:
  - `AGENT_REQUEST_COUNT` (`fleet_agent_requests_total`, labels
    `endpoint`/`has_agent_id`) — incremented in
    `resolve_agent_metadata()`, the one place already common to all
    three routes.
  - `AGENT_WORKFLOW_FAILURES` (`fleet_agent_workflow_failures_total`,
    label `endpoint`) — only counted when `workflow_id` was present (a
    plain caller's 503 isn't a workflow failure, there's no workflow).
    Also counts `/queued/generate`'s `{"error": ...}` result body, which
    currently still returns HTTP 200 (a separate, pre-existing issue this
    metrics-only phase didn't change) — the metric reports the real
    outcome regardless of what status code the response carries.
  - `CONTEXT_TOKENS` (`fleet_context_tokens`, histogram) — observed in
    `get_prospective_context_tokens()`, i.e. exactly the number that
    already drives routing decisions, not a separate estimate.
  - `CONTEXT_ITEMS_RECORDED` (`fleet_context_items_recorded_total`) —
    incremented inside `ContextStore.add()` itself (not at each call
    site), so it's accurate regardless of caller and can't drift as new
    callers get added later.
  - `CONTEXT_CAPACITY_REJECTIONS` (`fleet_context_capacity_rejections_total`)
    — needed a small but real refactor: `LoadBalancer.pick_worker()`
    previously raised a bare `RuntimeError` for both "no healthy
    workers" and "no worker has capacity," distinguishable only by
    string-matching the message. Added `NoCapacityError(RuntimeError)`
    so the two failure modes are precisely distinguishable (and existing
    `except RuntimeError` call sites keep working unchanged, since it's
    still a `RuntimeError`).
  - `INPUT_TOKENS_TOTAL`/`OUTPUT_TOKENS_TOTAL` (`fleet_input_tokens_total`/
    `fleet_output_tokens_total`, label `worker_url`) — Ollama already
    returns `prompt_eval_count`/`eval_count` on every response
    (`workers/ollama_client.py` was already extracting them into
    `prompt_tokens`/`completion_tokens` in the JSON body) — just wasn't
    exporting them as metrics until now. Real per-request data, not an
    estimate.
* Grafana dashboard (`observability/grafana/provisioning/dashboards/
  llm-engine.json`) — new "Agent / Context" row: agent-aware req/s,
  workflow failure rate, capacity rejection rate, context items
  recorded/s (4 stat panels), context-tokens-per-request percentiles and
  input/output tokens by worker (2 timeseries). Validated as parseable
  JSON; not opened in an actual Grafana instance (none running in this
  environment) — same honesty standard as the rest of this phase.
* **Verified live with real traffic**, not just unit tests: sent a plain
  request, an agent-aware request, and one deliberately oversized
  (capacity-rejected) request to a running gateway, then read `/metrics`
  directly. Every number matched exactly: `fleet_agent_requests_total`
  split 2 `has_agent_id="False"` / 1 `has_agent_id="True"`;
  `fleet_agent_workflow_failures_total{endpoint="/generate"}` = 1 (the
  rejection); `fleet_context_tokens_count` = 2 (only the two
  `workflow_id`-carrying requests); and — the important one —
  `fleet_context_items_recorded_total` = **1, not 2**, live proof the
  Phase-9-fix regression tests are backed by real behavior: the rejected
  oversized request still isn't recorded, even under real traffic.
* 12 new tests added (200 total): agent/context/capacity metrics via
  delta assertions against the real module-level Counter/Histogram
  singletons (`tests/test_metrics.py`), plus the first-ever direct unit
  tests of `OllamaClient` (`tests/test_ollama_client.py`, mocked
  `httpx.AsyncClient`) — token metrics increment on success, don't
  increment on failure, `NoCapacityError` is precisely distinguishable
  from a generic `RuntimeError`.

## Brutal live testing pass — Phase 13

Same discipline as the post-Phase-12 audit: real adversarial testing
against the running gateway, not just re-running green tests. No new
bugs found this round, but two things worth recording — one a genuine,
previously-unverified capability now confirmed, one a real characteristic
worth understanding before relying on it in production.

* **Concurrent load, exact correctness.** Ran `scripts/simulate.py
  --agents 4 --workload batch --duration 20` (real concurrent agents,
  `asyncio.gather`, not sequential) against the live gateway. Captured
  `/metrics` before and after: `fleet_agent_requests_total{endpoint="/chat",
  has_agent_id="True"}` +6, `fleet_context_items_recorded_total` +6 —
  both matching the simulator's own "6 requests, 0 failed" report
  exactly. No race, no double-count, no drop, under real concurrency.
* **Pathological input.** Sent a 400,000-character prompt
  (~100k estimated tokens) — 503 in 4ms, gateway unaffected, and
  `fleet_context_tokens_bucket{le="+Inf"}` incremented while
  `le="32768.0"` didn't, confirming correct histogram bucketing at the
  extreme end.
* **Confirmed, for the first time, a capability `CLAUDE.md` has
  documented since before this redesign started but nobody had actually
  tested: "scale gateway replicas (not uvicorn `--workers`) to add
  consumers."** Started a second full gateway process on `:8001`,
  independently, with zero manual coordination — it connected to the
  same `llm:stream:request_queue`/`fleet-workers` group on its own. Fired
  4 concurrent `/queued/generate` requests at replica 1; the two
  replicas' logs show jobs 1 & 4 processed by replica 1, jobs 2 & 3 by
  replica 2 — real Redis Streams consumer-group load balancing, working
  correctly, live, across two genuinely separate OS processes. Locked in
  as `test_two_worker_pools_share_queue_without_duplicate_processing`
  (201 total): two `WorkerPool` instances race for the same 8-job queue,
  and the shared call counter proves each job is processed by exactly
  one of them — not zero, not two.
* **Real characteristic worth knowing, not a bug**: each replica's
  `/metrics` only reflects work done in *that* process. Confirmed live —
  replica 1 (which received all 4 HTTP POSTs) shows
  `fleet_agent_requests_total{endpoint="/queued/generate"}` = 5, replica
  2 shows nothing for that metric despite having actually processed 2 of
  those 4 jobs (its `llm_worker_requests_total`/`fleet_input_tokens_total`
  *did* increment for the real work it did — the split is specifically
  between "received the HTTP request" metrics on one replica and
  "dequeued and processed the job" metrics potentially on a different
  one). This is standard Prometheus multi-instance behavior — true
  fleet-wide totals need server-side `sum() by (...)` aggregation across
  every replica's target — but `observability/prometheus/prometheus.yml`
  currently only lists two fixed targets (compose service name +
  `host.docker.internal`), not a scrape config that scales to N replicas.
  Not fixed here (out of scope for a testing pass), just flagged
  honestly: anyone actually running multiple replicas needs to extend
  that scrape config, or the numbers on any single replica's dashboard
  will undercount real fleet-wide activity.

## Phase 14 — benchmarks

Per REDESIGN.md §52-61/§72 Phase 14. Runs only **3 of REDESIGN.md's 6**
named experiments — see `docs/experiments.md`'s scope note for why: §53
(full history vs. budgeted), §54 (tool output explosion), and §56
(memory retrieval) all compare context *strategies applied to what's
sent to a model*, but nothing in the live gateway applies context
selection/artifacts/memory to outgoing prompts yet (same gap Phase 13's
audit already flagged). Faking that comparison would misrepresent the
system rather than measure it — so those three aren't run. What's
runnable and genuinely real:

* **Experiment 1** (`benchmarks/experiments/context_budgeting.py`,
  adapted from §53) — the closest honest equivalent available today:
  calls `context/selection.py` directly (not through the gateway) against
  a real task (Phase 11's planted calculator bug) and a real model.
  "Task success" is an objective keyword check on the model's diagnosis
  (§61 — not an LLM judge). **Real result**: `hybrid` (400-token budget)
  used 396 actual tokens vs. `full`'s 629 (39% fewer) and was 34% faster,
  both correctly diagnosed the bug.
* **Experiment 5** (`context_aware_routing.py`, §57) — runs the real
  production `LoadBalancer.pick_worker()` path against synthetic workers
  matching §41's own example exactly (this environment has one real
  Ollama instance, §57 needs several with different capacities). All 3
  scenarios matched §41's expected eligibility exactly, including the
  busy-but-capable worker staying eligible and the oversized-request
  rejection.
* **Experiment 6** (`agent_bursts.py`, §58) — reuses the Phase 12
  simulator directly against the live gateway. Deliberately small scales
  (2, 4 agents, not §58's 10-500) — this environment has one CPU-only
  Ollama worker in WSL; larger scales wouldn't finish in a reasonable
  time and the throughput ceiling is capacity-bound by that one worker
  regardless. Zero failures at both scales; P50 latency roughly doubled
  from 2→4 agents, consistent with real single-worker queueing.
* `benchmarks/runner.py` — single entrypoint (`--only <name>` to run
  just one), matching REDESIGN.md §71's `benchmarks/runner.py`.
* `docs/experiments.md` — hypothesis/setup/variables/metrics/results/
  limitations/conclusion for each of the 3, per §61's template. Every
  number is from an actual run captured during this phase, not
  projected or estimated (§39/§60's standing rule).
* 9 new tests added (210 total): pure-logic tests for each experiment's
  non-network helpers (`build_context_pool`/`task_succeeded` for
  Experiment 1; the full 3-scenario matrix for Experiment 5, since it has
  no network dependency at all and can be fully exercised in the suite).
  Experiment 6 has no dedicated tests — it's pure orchestration over
  already-tested `scripts/simulate.py` functions (`assign_workloads`,
  `run_agent`, `percentile`), same as Experiment 1/5's own underlying
  library code (`context/selection.py`, `router/load_balancer.py`) is
  already covered by their own phases' test suites.

## Brutal live testing pass — Phases 14 and 15

Same procedure as the Phase 13 pass: real adversarial testing, not just
re-running the suite. No critical bugs found — Phase 14's numbers and
Phase 15's docs held up.

* **Static re-audit**: re-ran the exact greps from the Phase 12/13
  audits (`context_manager`/`memory_manager` live usage,
  `MAX_TOKENS`/`CONTEXT_BUDGET_DEFAULT`/`CONTEXT_SELECTION_POLICY` dead
  settings) against current `HEAD`. Every claim in the new
  `docs/architecture.md`/`docs/context.md`/`docs/memory.md`/`README.md`
  still matches reality exactly.
* **Full `benchmarks/runner.py` run** (never tested end-to-end before —
  only individual experiments had been run): all 3 experiments back to
  back, mixing the sync `context_aware_routing.run()` with the async
  `context_budgeting.run()`/`agent_bursts.run()` calls correctly, no
  crash. Re-ran Experiment 1 for a second time as a side effect —
  produced a second real, independent 39%-fewer-tokens result (629→396
  tokens again, both diagnoses correct), reinforcing it wasn't a fluke
  of the first run.
* **Adversarial misuse**: ran `agent_bursts.py --base-url
  http://localhost:9999` (nothing listening). Confirmed via a direct
  `curl` that connection-refused fails in ~30ms, not slow — so the ~40s
  it took the experiment to finish isn't a hang, it's `run_agent()`
  correctly retrying for the full configured `--duration` regardless of
  failure. **Real characteristic found**: with a fully dead target and
  the `batch` workload profile's near-zero think-time, this produces no
  backoff — 411 requests hammering a provably-unreachable endpoint in
  20 seconds. Not fixed (it's a benchmark tool retrying a target it has
  no way to know is permanently dead, not a production code path;
  "should a load-testing tool back off when 100% of requests fail" is a
  legitimate design question, not an obvious bug) — flagged here so it's
  a documented, deliberate non-fix rather than an unnoticed gap.
* **Documentation link/reference check**: every `[text](path)` markdown
  link across `README.md`/`CLAUDE.md`/all of `docs/*.md` resolves to a
  real file (scripted check, not manual spot-checking). Cross-checked
  Experiment 1's numbers (629/396/39%/34%/22.7s/34.3s) are identical,
  digit-for-digit, everywhere they're quoted (`README.md`,
  `docs/experiments.md`, this file) — no transcription drift between
  copies.
* **Documented instructions actually work**: ran the exact
  `./.venv/bin/python scripts/smoke_test.py --model llama3:latest` and
  `./.venv/bin/pytest tests/ -q` commands as literally written in the
  new `README.md`/`CLAUDE.md` — 9/9 and 210/210 respectively.
