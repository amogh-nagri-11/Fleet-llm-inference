# CLAUDE.md

Guidance for working in this repo.

## What this is

Fleet — an agent context & inference runtime. FastAPI gateway → context-aware
load balancer → Ollama workers, with a Redis Streams queue (consumer groups,
crash recovery, dead-letter queue), circuit breaker, health checks, autoscaling,
agent/workflow-aware Prometheus/Grafana observability, and a context/memory
subsystem (see caveat below). See `README.md` for the architecture diagram,
`docs/architecture.md` for the full system design, and `docs/deployment.md` for
deploy paths.

**Important, easy to miss**: `context/` (selection, artifacts, compression) and
`memory/` (storage, retrieval) are real, tested, and — for memory — connected
in the app lifespan, but **no route in `gateway/routes.py` actually invokes
them**. Only context *recording* + *token counting* (feeding routing decisions)
is live. Don't assume a route does budget-aware selection, artifact
externalization, compression, or memory retrieval just because the code
exists — check `gateway/routes.py` first. See `docs/architecture.md` §14 and
`docs/migration-plan.md`'s Phase 13 entry for the full picture.

## Layout

- `gateway/` — FastAPI app. `main.py` (lifespan + app, connects Redis/Postgres),
  `routes.py` (endpoints, prefixed `/api/v1`), `middleware.py` (per-request
  metrics), `metrics.py` (single source of truth for all Prometheus metrics —
  `llm_*` pre-redesign, `fleet_*` agent/context, added Phase 13).
- `router/` — `load_balancer.py` (strategies + context-capacity pre-filter +
  runtime add/remove worker; raises `NoCapacityError` for capacity rejections,
  a `RuntimeError` subclass), `circuit_breaker.py`, `health_checker.py` (15s
  loop), `autoscaler.py`.
- `workers/` — `ollama_client.py` (wraps Ollama `/api/chat`, tracks stats +
  token metrics), `worker_pool.py` (Redis Streams consumer + recovery loop, via
  `job_queue/`; horizontally scalable — verified with two real replicas).
- `context/` — `models.py` (`ContextItem`/`ContextType`), `store.py`
  (in-memory, per-workflow, no expiry), `manager.py` (`ContextManager`,
  imported by `gateway/routes.py`), `selection.py` (5 budget policies),
  `artifacts.py` (large-content externalization), `compression.py` (LLM
  summarization — the one place `context/` calls the inference layer). See the
  "important, easy to miss" note above.
- `memory/` — `models.py` (`MemoryItem`/`MemoryKind`, working+episodic only —
  semantic is deferred), `store.py` (PostgreSQL), `manager.py`
  (`MemoryManager`, connected in `gateway/main.py`'s lifespan), `ranking.py`,
  `retrieval.py`.
- `job_queue/` — `streams.py` (Redis Streams wrapper), `retry.py`
  (`XAUTOCLAIM`-based recovery), `dead_letter.py`. Named `job_queue/`, not
  `queue/` — `queue` is a Python stdlib module, using it would shadow the
  standard library.
- `client/fleet_client.py` — thin HTTP SDK, used by `examples/coding_agent/`
  and `scripts/simulate.py`.
- `examples/coding_agent/` — reference agent (real tools, real sandbox repo,
  real LLM calls) — deliberately simple, not a framework.
- `benchmarks/` — REDESIGN.md experiment implementations + `runner.py`.
- `config/settings.py` — all config via `os.getenv` (no pydantic-settings).
- `observability/` — Prometheus config + auto-provisioned Grafana dashboard
  (now includes an "Agent / Context" panel row).
- `k8s/` — manifests + KEDA ScaledObjects. **Not updated with a Postgres
  deployment** when `memory/` was added — the k8s path currently only covers
  the inference-gateway portion of Fleet.

## Conventions

- Config is plain `os.getenv` in `config/settings.py`. Add new settings there and
  document them in `.env.example`.
- All Prometheus metrics live in `gateway/metrics.py` — import from there, don't
  redefine `Counter`/`Gauge`/`Histogram` elsewhere (label sets must stay unique).
- Redis queue is a Stream at `llm:stream:request_queue` (consumer group
  `fleet-workers`, see `job_queue/`), not the pre-Phase-10 List at
  `llm:request_queue`; result keys are still `llm:result:<id>`.
- Routes live under the `/api/v1` prefix; `POST` routes require the `x-api-key`
  header (`config.settings.API_KEY`).

## Running locally

Requires Ollama (`:11434`), Redis (`:6379`), and PostgreSQL on the host (native
install, not a container — see below). Docker is **not** available in this WSL
environment — `docker`/`docker compose` commands fail here. The venv is
`.venv/`, not `venv/`.

```bash
# one-time: Postgres for memory storage (native install, peer auth)
sudo apt-get install postgresql
sudo -u postgres createuser -s $(whoami)
sudo -u postgres createdb -O $(whoami) fleet

# gateway (use the venv)
STANDBY_WORKER_URLS=http://localhost:11435 \
  ./.venv/bin/uvicorn gateway.main:app --host 0.0.0.0 --port 8000

# a second worker for autoscaler testing
OLLAMA_HOST=0.0.0.0:11435 ollama serve

# smoke test (exercises the queued path -> real Redis Streams)
./.venv/bin/python scripts/smoke_test.py --model llama3:latest

# benchmark
./.venv/bin/python scripts/benchmark.py -n 8 -c 4

# tests (pytest.ini scopes collection to tests/ only — see Gotchas)
./.venv/bin/pytest tests/ -q
```

## Autoscaler

`router/autoscaler.py` has two modes: Docker container spin-up (when a daemon is
reachable) and a **static-registration fallback** that registers
`STANDBY_WORKER_URLS` into the load balancer at runtime. On Kubernetes, KEDA owns
scaling instead and the in-app autoscaler is disabled via the ConfigMap.

## Gotchas

- The gateway Docker image **must be built from the repo root** (`docker build -f
  gateway/Dockerfile .`), not from `gateway/`, because it imports the top-level
  `config`/`router`/`workers` packages.
- The `WorkerPool` consumer loop processes the queue serially per gateway
  process; scale gateway replicas (not uvicorn `--workers`) to add consumers —
  this is genuinely load-balanced via the shared Redis Streams consumer group,
  verified live with two real gateway processes (see `docs/migration-plan.md`'s
  Phase 13 brutal-testing entry).
- `print()`-based logging is buffered when stdout is redirected to a file —
  log lines can lag real execution by an unpredictable amount (confirmed live
  during a crash-recovery test: a job that had already fully completed didn't
  show its log lines until a later, unrelated request triggered a flush). Not
  fixed; if you're debugging a live issue by tailing a redirected log, don't
  trust "nothing happened yet."
- `pytest.ini` sets `testpaths = tests` — without it, a bare `pytest` run from
  the repo root would also collect `examples/coding_agent/sandbox_repo/
  test_calculator.py`, a deliberately-buggy demo fixture, as if it were part
  of Fleet's real suite.
- Several settings are defined and documented but genuinely dead code — never
  read anywhere outside `config/settings.py`/tests: `MAX_TOKENS`,
  `CONTEXT_BUDGET_DEFAULT`, `CONTEXT_SELECTION_POLICY`. Don't assume setting
  them does anything until `context/selection.py` is wired into a live route.
- `context_manager`/`memory_manager` are real module-level singletons shared
  by the whole gateway process — recording into them before confirming a
  worker can actually handle the request was a real, live-reproduced bug
  (permanently bricked a workflow). Fixed (`gateway/routes.py`'s
  `get_prospective_context_tokens`/`record_context` split), but worth knowing
  the failure mode existed if you're adding a new code path that touches
  either singleton: measure before you commit to recording.
