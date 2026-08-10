# CLAUDE.md

Guidance for working in this repo.

## What this is

A distributed LLM inference gateway: FastAPI gateway → load balancer → Ollama
workers, with a Redis queue, circuit breaker, health checks, autoscaling, and
Prometheus/Grafana observability. See `README.md` for the architecture diagram
and `docs/deployment.md` for deploy paths.

## Layout

- `gateway/` — FastAPI app. `main.py` (lifespan + app), `routes.py` (endpoints,
  prefixed `/api/v1`), `middleware.py` (per-request metrics), `metrics.py`
  (single source of truth for all Prometheus metrics).
- `router/` — `load_balancer.py` (strategies + runtime add/remove worker),
  `circuit_breaker.py`, `health_checker.py` (15s loop), `autoscaler.py`.
- `workers/` — `ollama_client.py` (wraps Ollama `/api/chat`, tracks stats),
  `worker_pool.py` (Redis Streams consumer + recovery loop, via `job_queue/`).
- `config/settings.py` — all config via `os.getenv` (no pydantic-settings).
- `observability/` — Prometheus config + auto-provisioned Grafana dashboard.
- `k8s/` — manifests + KEDA ScaledObjects.

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

Requires Ollama (`:11434`) and Redis (`:6379`) on the host. Docker is **not**
available in this WSL environment — `docker`/`docker compose` commands fail here.

```bash
# gateway (use the venv)
STANDBY_WORKER_URLS=http://localhost:11435 \
  ./venv/bin/uvicorn gateway.main:app --host 0.0.0.0 --port 8000

# a second worker for autoscaler testing
OLLAMA_HOST=0.0.0.0:11435 ollama serve

# benchmark
./venv/bin/python scripts/benchmark.py -n 8 -c 4
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
  process; scale gateway replicas (not uvicorn `--workers`) to add consumers.
