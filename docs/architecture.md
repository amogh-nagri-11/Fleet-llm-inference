# Fleet — Architecture

Per REDESIGN.md §75. Describes the system as it actually exists after
Phases 1-14 of the redesign — not the aspirational end state. Where a
described capability isn't wired into live request handling yet, that's
called out explicitly rather than implied.

## 1. Problem

Agentic applications don't make a single LLM request — an agent
accumulates conversation history, tool outputs, files, errors, and
memories across many steps, and eventually that context exceeds what a
model can accept. Fleet's job is to sit between agents and inference
backends and decide what context should be included, what should be
retrieved, what should be discarded, and which model worker should
process the resulting request — without becoming an agent framework or a
model itself. See `REDESIGN.md` §2 for the full motivating example.

## 2. Design goals

- **Context is a resource**, not an afterthought — token budgets,
  selection, and compression are first-class concerns (`context/`).
- **Durable memory** for agents that persist across requests, separate
  from per-workflow context (`memory/`).
- **Reliability inherited, not rebuilt** — the pre-redesign gateway's
  circuit breaker, health checks, and load balancing stay in place; the
  redesign adds context/memory on top rather than replacing the
  inference layer (`REDESIGN.md` §0.1's supersession note).
- **Deterministic where possible** — context selection and artifact
  summarization don't call an LLM (§10); the one place Fleet does call a
  model for its own purposes (compression, Phase 8) is explicit about it.
- **Measure, don't assert** — every phase of this redesign shipped with
  tests and, where relevant, live verification against a real gateway and
  a real model. `docs/experiments.md` extends that discipline to
  benchmarks: only real, run numbers, never projected ones.

## 3. Architecture

```
                    Agent (real or simulated)
                            |
                            v
                    client/fleet_client.py  (thin SDK, §64)
                            |
                            v
                 gateway/routes.py  (/api/v1/*)
                            |
              +-------------+-------------+
              |                           |
              v                           v
    context/manager.py            router/load_balancer.py
    (record + count tokens,     (context-aware pick_worker,
     Phase 9 — live)             circuit breaker, Phase 1-9)
              |                           |
              |                           v
              |                  workers/ollama_client.py
              |                           |
              |                           v
              |                    Ollama (real model)
              |
              v
    context/selection.py, context/artifacts.py, context/compression.py
    memory/manager.py, memory/retrieval.py
    (built, tested, NOT called from the live request path — see §4)
```

Async request path (`/queued/generate`): `gateway/routes.py` enqueues
onto `job_queue/streams.py` (a real Redis Stream with a consumer group);
`workers/worker_pool.py` consumes, dispatches through the same
`load_balancer`, and can run as multiple independent replicas against
the same queue (verified live — see the Phase 13 brutal-testing entry in
`docs/migration-plan.md`).

## 4. Request lifecycle

For `/generate` and `/chat` (the synchronous paths):

1. `verify_api_key` — 401 if missing/wrong.
2. `resolve_agent_metadata` — extracts `agent_id`/`workflow_id`/
   `request_id`/`parent_request_id` (all optional), generates a
   `request_id` if the caller didn't supply one. Increments
   `fleet_agent_requests_total`.
3. **If `workflow_id` is present**: `get_prospective_context_tokens`
   computes what the workflow's total context would become *without
   recording it yet* (this split exists specifically because recording
   before admission was a real, live-reproduced bug — see the Phase 9
   fix entry in `docs/migration-plan.md`).
4. `load_balancer.pick_worker(context_tokens=...)` — filters out workers
   whose `max_context_tokens` can't hold the request; raises
   `NoCapacityError` (a `RuntimeError` subclass) if none qualify → 503.
5. Only now, with a worker actually secured, `record_context` persists
   the request into `context_manager` as a `CONVERSATION` item.
6. The worker call happens; on success the result is returned with the
   resolved metadata merged in; on failure the circuit breaker is
   notified and a 503 is returned.

`/queued/generate` follows the same admission logic, but deferred:
`context_tokens` is computed (not recorded) at enqueue time, and the
actual `pick_worker()` + `record_context` happen inside
`worker_pool.py`'s `_process_job()` at dispatch time — which may be on a
different replica than the one that received the HTTP request (Phase 13
brutal-testing finding: this means a single logical request's metrics can
legitimately be split across two replicas' `/metrics` endpoints).

## 5. Context lifecycle

`ContextItem` (`context/models.py`) → `ContextStore.add()` (in-memory,
per-`workflow_id`, no expiry) → optionally selected down to a budget
(`context/selection.py`, not live-wired) → optionally externalized as an
`Artifact` if large (`context/artifacts.py`, not live-wired) → optionally
compressed into a summary via a real model call
(`context/compression.py`, not live-wired). See `docs/context.md` for
the full design and exactly which of these stages actually run today.

## 6. Memory lifecycle

`MemoryItem` (`memory/models.py`, working or episodic — semantic is
deferred, §0.2) → `MemoryStore` (real PostgreSQL, connected in
`gateway/main.py`'s lifespan as of the Phase 13 fix) → ranked and
budgeted on retrieval (`memory/ranking.py`, `memory/retrieval.py`) →
converted to `ContextItem`s (`type=MEMORY`) ready for injection. The
storage and retrieval pipeline is real and tested end-to-end against a
live database; **no route calls it** — see `docs/memory.md`.

## 7. Inference lifecycle

Retained from the pre-redesign gateway, extended with context-awareness
in Phase 9: `LoadBalancer` picks a healthy, context-capable
`OllamaClient`; the circuit breaker (`router/circuit_breaker.py`) tracks
per-worker failures and opens after `CIRCUIT_BREAKER_THRESHOLD`
consecutive failures, half-opens after `CIRCUIT_BREAKER_TIMEOUT`;
`router/health_checker.py` polls every 15s in the background.

## 8. Worker architecture

One `OllamaClient` per configured `WORKER_URLS` entry, each with a
`max_context_tokens` (uniform, from `WORKER_MAX_CONTEXT_TOKENS` — this
codebase has no per-worker capability registry; every worker is assumed
to run the same model). vLLM support is an explicitly deferred interface
stub (§0.2) — Ollama is the only implemented backend.

## 9. Scheduling

Three routing strategies (`ROUTING_STRATEGY`): `round_robin`,
`least_latency`, `queue_depth` — all pre-redesign, unchanged. Phase 9
adds a capacity *pre-filter* ahead of whichever strategy is active,
rather than a competing strategy: `pick_worker(context_tokens=...)`
narrows the candidate set to workers with enough capacity, then the
configured strategy picks among those. A worker that's merely busy but
capable stays eligible (§41) — only hard capacity excludes a worker.

## 10. Reliability

- **Circuit breaker** — per worker, CLOSED → OPEN → HALF_OPEN, unchanged
  from the pre-redesign gateway.
- **Reliable queue** (Phase 10) — Redis Streams with a consumer group
  (`job_queue/streams.py`), `XAUTOCLAIM`-based recovery of jobs whose
  consumer died mid-processing (`job_queue/retry.py`), a dead-letter
  queue after `QUEUE_MAX_RETRIES` (`job_queue/dead_letter.py`).
  Idempotency comes from the existing `request_id`-keyed result store —
  reprocessing a reclaimed job just overwrites the same key.
- **Horizontal scaling** — multiple `WorkerPool` instances (i.e. gateway
  replicas) share one consumer group with zero coordination beyond Redis
  itself. Verified live with two real gateway processes (Phase 13
  brutal-testing entry).
- **Autoscaling** — unchanged from the pre-redesign gateway: Docker
  container spin-up when available, static `STANDBY_WORKER_URLS`
  registration otherwise (this WSL dev environment has no Docker daemon,
  so static mode is what's actually exercised here).

## 11. Observability

`fleet_`-prefixed metrics added in Phase 13 alongside the pre-redesign
`llm_`-prefixed ones (not renamed, to avoid breaking the existing
dashboard): `fleet_agent_requests_total`, `fleet_agent_workflow_failures_total`,
`fleet_context_tokens` (histogram), `fleet_context_items_recorded_total`,
`fleet_context_capacity_rejections_total`, `fleet_input_tokens_total`/
`fleet_output_tokens_total`. Deliberately not labeled by `agent_id`/
`workflow_id` (unbounded cardinality); per-agent detail is in the
structured `[Fleet] event=received ...` logs instead. See
`gateway/metrics.py` for the full list of metrics explicitly *not*
added, and why (mostly: nothing live calls the code they'd measure).

## 12. Security

- `x-api-key` header required on all `POST` routes (`config.settings.API_KEY`).
- No rate limiting, no request-size caps. `MAX_TOKENS` is defined in
  `config/settings.py` and documented in `.env.example` but — checked
  before writing this — never read anywhere else in the codebase; it's
  dead config, the same category as `CONTEXT_BUDGET_DEFAULT`/
  `CONTEXT_SELECTION_POLICY` flagged in the Phase 12 brutal audit.
- Memory has no cross-agent isolation enforcement beyond what callers
  choose to pass as `workflow_id`/`agent_id` — nothing prevents one
  workflow's code from reading another's memory if it knows the
  `workflow_id` string. `REDESIGN.md` §69 describes scoped isolation;
  it isn't implemented.

## 13. Tradeoffs

- **In-memory `ContextStore`/`ArtifactStore`, durable `MemoryStore`** —
  context is scoped to a single gateway process's lifetime (lost on
  restart); memory persists in Postgres. This was a deliberate,
  documented choice per phase (Phase 3/5 vs. Phase 6), not an oversight.
- **`job_queue/` naming, not REDESIGN.md's literal `queue/`** — `queue`
  is a Python stdlib module; using it would've shadowed the standard
  library for every dependency importing it. Confirmed the collision
  before renaming (Phase 10).
- **Fleet-wide metric aggregates, not per-agent labels** — chosen over
  cardinality risk; the tradeoff is losing at-a-glance per-agent
  dashboards in exchange for a metrics backend that doesn't fall over.

## 14. Limitations

- Context selection, artifact externalization, compression, and memory
  retrieval are all built and tested but **not invoked by any live
  route** — see `docs/context.md` and `docs/memory.md` for exactly what
  that means in practice.
- No streaming — responses are single-shot, so TTFT/TPOT can't be
  measured (not just "not implemented," genuinely not observable without
  it).
- `ContextStore` has no expiry or size cap — a long-running workflow
  accumulates context forever within a process's lifetime. (The specific
  bug where *rejected* requests still got recorded is fixed; the general
  "nothing ever expires" property is not.)
- No admission control, rate limiting, or request-size limits (§21-22 of
  the superseded scheduling draft; not part of this document's scope
  either).
- Single-instance-only reliability testing: consumer-group sharing was
  verified with two replicas; behavior at higher replica counts, or with
  a real multi-worker Ollama fleet, is untested (this environment has
  exactly one Ollama instance).
- `vLLM`, semantic memory (embeddings/vector search), the full
  `FleetAgent` SDK wrapper, and cost-based budgets are explicitly out of
  scope for this redesign (`REDESIGN.md` §0.2) — not partially built,
  not planned for a later phase within this effort.

## 15. Future work

- Wire `context/selection.py`, `context/compression.py`, and
  `memory/retrieval.py` into the live request path — the single biggest
  gap between what's built and what's actually reachable. This is what
  would let Experiments §53/§54/§56 (`docs/experiments.md`'s scope note)
  become real comparisons instead of library-level ones.
- Streaming support, to make TTFT/TPOT measurable.
- A worker capability registry for genuinely heterogeneous workers
  (different models/context limits per worker, not a uniform
  `WORKER_MAX_CONTEXT_TOKENS`).
- Prometheus scrape config that scales to N replicas, so per-agent
  metrics aggregate correctly under horizontal scaling.
- Everything listed in §0.2 as explicitly deferred: vLLM, semantic
  memory, the full agent SDK, cost-based budgets.
