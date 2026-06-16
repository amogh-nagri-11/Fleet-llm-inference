# LLM Inference Engine

A distributed inference engine that sits between your application and LLM models —
managing load balancing, queuing, circuit breaking, autoscaling, and
observability at production scale. Workers are [Ollama](https://ollama.com)
instances; the gateway is FastAPI.

## Architecture

```
                         ┌──────────────┐
          x-api-key ───► │  API Gateway │  FastAPI · :8000 · /metrics
                         │  (FastAPI)   │
                         └──┬────────┬──┘
              sync path     │        │   queued path
        /generate /chat     │        │   /queued/generate
                            │        ▼
                            │   ┌──────────┐  RPUSH llm:request_queue
                            │   │  Redis   │◄─────────────┐
                            │   │  queue   │              │
                            │   └────┬─────┘         WorkerPool
                            │        │ BLPOP         (consumer loop)
                            ▼        ▼                    │
                    ┌───────────────────────┐            │
                    │     Load Balancer      │◄───────────┘
                    │ round_robin /          │
                    │ least_latency /        │   ┌─────────────────┐
                    │ queue_depth            │   │ Circuit Breaker │ per worker
                    └──┬─────────┬─────────┬─┘   │ Health Checker  │ every 15s
                       │         │         │     └─────────────────┘
                  ┌────▼───┐ ┌───▼────┐ ┌──▼─────┐
                  │Ollama 1│ │Ollama 2│ │Ollama N│
                  └────────┘ └────────┘ └────────┘
                       ▲
        Autoscaling ───┘
        • In-app autoscaler (Docker spin-up, or static-registration
          fallback for WSL/bare hosts) — watches queue depth
        • KEDA ScaledObject on Redis LLEN (Kubernetes)

        Observability: Prometheus scrapes /metrics → Grafana dashboard
```

## Tech Stack

| Layer | Technology |
|---|---|
| API Gateway | FastAPI, Python 3.11 |
| Queue | Redis |
| Inference Workers | Ollama |
| Observability | Prometheus, Grafana |
| Orchestration | Docker Compose → Kubernetes |
| Autoscaling | In-app autoscaler + KEDA |

## Project Structure

```
llm-inference-engine/
├── gateway/         # FastAPI app: routes, auth, middleware, metrics
├── router/          # Load balancer, circuit breaker, health checker, autoscaler
├── workers/         # Ollama client + Redis-backed worker pool
├── observability/   # Prometheus config + auto-provisioned Grafana dashboard
├── config/          # Settings (env-driven)
├── scripts/         # Setup + benchmark
├── k8s/             # Kubernetes manifests + KEDA ScaledObjects
└── docs/            # Architecture, API, deployment
```

## Getting Started

### Local (host)

```bash
cp .env.example .env            # then edit values

# dependencies running on the host
ollama serve &                  # worker on :11434  (ollama pull llama3)
redis-server &                  # queue

pip install -r gateway/requirements.txt
uvicorn gateway.main:app --reload
```

Smoke test:

```bash
curl -s -X POST localhost:8000/api/v1/generate \
  -H "x-api-key: dev-key" -H 'content-type: application/json' \
  -d '{"prompt":"In one sentence, what is a load balancer?"}'
```

### Docker Compose

```bash
docker compose up --build                              # dev (hot reload)
docker compose -f docker-compose.prod.yml up -d --build   # single-machine prod
```

- Gateway: http://localhost:8000 (`/docs`, `/metrics`)
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin / admin) — the **LLM Inference Engine**
  dashboard loads automatically.

### Kubernetes + KEDA

See [`k8s/README.md`](k8s/README.md) and [`docs/deployment.md`](docs/deployment.md)
(includes RunPod / Railway / Fly.io notes).

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/generate` | Single prompt, routed directly to a worker |
| `POST` | `/api/v1/chat` | Multi-message chat completion |
| `POST` | `/api/v1/queued/generate` | Enqueue via Redis, await result |
| `GET`  | `/api/v1/health` | Status, queue depth, worker stats |
| `GET`  | `/api/v1/workers` | Per-worker health / latency / circuit state |
| `GET`  | `/api/v1/queue/depth` | Current queue depth |
| `GET`  | `/metrics` | Prometheus metrics |

All `POST` routes require the `x-api-key` header.

## Autoscaling

The in-app autoscaler (`router/autoscaler.py`) watches the Redis queue depth and
scales workers when `depth >= AUTOSCALE_UP_THRESHOLD`:

- **Docker mode** — spins up `ollama/ollama` containers when a Docker daemon is
  reachable.
- **Static fallback** — when Docker is unavailable (e.g. WSL), it registers
  pre-started Ollama instances from `STANDBY_WORKER_URLS` into the load balancer
  at runtime, and deregisters them when the queue drains. Tunables:
  `AUTOSCALE_{MIN,MAX}_WORKERS`, `AUTOSCALE_{UP,DOWN}_THRESHOLD`,
  `AUTOSCALE_CHECK_INTERVAL`, `AUTOSCALE_COOLDOWN`.

On Kubernetes, KEDA scales the Ollama and gateway Deployments on Redis `LLEN`
instead (see `k8s/40-keda-scaledobject.yaml`).

## Benchmark

`scripts/benchmark.py` fires concurrent requests and reports throughput and
p50/p95/p99 latency:

```bash
python scripts/benchmark.py -n 50 -c 10
python scripts/benchmark.py -n 50 -c 10 --endpoint /api/v1/queued/generate
```

### Reference results

Single Ollama worker, `llama3:latest` (8B, Q4_0) on **CPU** (WSL2), short prompt,
8 requests at concurrency 4:

| Metric | Value |
|---|---|
| Requests | 8 (8 ok / 0 failed) |
| Throughput | 0.19 req/s |
| Latency p50 | 17,619 ms |
| Latency p95 | 23,994 ms |
| Latency p99 | 25,187 ms |

> CPU inference of an 8B model is the bottleneck here — these numbers measure the
> worker, not the gateway. On GPU workers latency drops by ~10–50× and throughput
> scales with worker (and gateway-consumer) replicas. Re-run the benchmark in
> your target environment to get representative figures.

## Phases

- [x] Phase 1 — Gateway + routing to Ollama workers
- [x] Phase 2 — Prometheus + Grafana observability
- [x] Phase 3 — Redis queue, health checks, circuit breaker
- [x] Phase 4 — Autoscaling (Docker + static-registration fallback)
- [x] Phase 5 — Kubernetes + KEDA + cloud deploy
