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
