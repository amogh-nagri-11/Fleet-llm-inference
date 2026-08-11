# Fleet — Agent Context & Inference Runtime

> An infrastructure runtime for agentic AI applications that manages
> agent context and memory under finite token budgets while routing
> inference across available model workers.

**Fleet does not implement an LLM or replace inference engines like
Ollama or vLLM.** It operates above them as a context, workflow, and
inference orchestration layer for agentic workloads. If you're looking
for the model runtime itself, that's [Ollama](https://ollama.com) — Fleet
is what sits between your agent and it.

```
Agent
  |
  v
Fleet
  |
  +--> retrieves/records context, per workflow
  |
  +--> checks a worker actually has capacity before admitting the request
  |
  +--> routes inference to a healthy, capable worker
  |
  +--> handles worker failures (circuit breaker, reliable queue, recovery)
  |
  +--> tracks agent/workflow/context metrics
  |
  v
Model worker (Ollama)
```

This is a redesign of an earlier "distributed LLM inference engine"
project — that positioning was accurate but incomplete: a load balancer
in front of Ollama is a common pattern. What's more specific to agentic
workloads is context *accumulating* across a long-running session until
it exceeds what a model can accept, and that's what this redesign adds
on top of the original gateway/router/worker foundation, not instead of
it. `docs/migration-plan.md` has the full phase-by-phase record of what
was kept, replaced, and why — including bugs found and fixed by testing
each phase against a real running gateway and a real model, not just
unit tests.

## Status: what's real vs. what's built-but-not-wired

This matters enough to put before the architecture diagram. As of this
redesign:

**Live, in the request path, verified against a real gateway + real
model:**
- Agent/workflow identity (`agent_id`/`workflow_id`/`request_id`) on
  every request.
- Context *recording* and *token counting* — every agent-aware request
  is recorded, and a workflow's running context size feeds routing.
- Context-*aware routing* — a worker without enough capacity is never
  selected, regardless of load.
- The reliable queue (Redis Streams, consumer groups, crash recovery,
  dead-letter queue) — including genuine multi-replica horizontal
  scaling, verified with two independent gateway processes.
- Agent/workflow/context Prometheus metrics.

**Built, tested (including against real Postgres/Redis/Ollama), but
*not* invoked by any live route yet:**
- Budget-aware context *selection* (`context/selection.py`) — the actual
  trimming-to-fit-a-budget logic. Demonstrated for real in
  `docs/experiments.md` (Experiment 1), just not triggered by an actual
  HTTP request today.
- Large-content externalization (`context/artifacts.py`).
- LLM-based context compression (`context/compression.py`).
- Durable working/episodic memory storage and retrieval
  (`memory/manager.py`, `memory/retrieval.py`) — real PostgreSQL, real
  ranking, real budgeting, connected in the app lifespan, just not
  called by a route.

Closing that gap — wiring selection/artifacts/compression/memory into
what actually gets sent to a model — is the single largest item in
`docs/architecture.md`'s Future Work section. Everything above is stated
plainly rather than implied, because a redesign that quietly claims more
than it does is worse than one that's specific about what's left.

## Architecture

```
                    Agent (real or simulated)
                            |
                            v
                    client/fleet_client.py  (thin SDK)
                            |
                            v
                 gateway/routes.py  (/api/v1/*)
                            |
              +-------------+-------------+
              |                           |
              v                           v
    context/manager.py            router/load_balancer.py
    (record + count tokens)      (context-aware pick_worker,
              |                    circuit breaker)
              |                           |
              |                           v
              |                  workers/ollama_client.py
              |                           |
              |                           v
              |                       Ollama
              |
              v
    context/selection.py, context/artifacts.py, context/compression.py
    memory/manager.py, memory/retrieval.py
    (real, tested — see "what's built-but-not-wired" above)
```

Async path (`/queued/generate`): requests go onto a real Redis Stream
(`job_queue/streams.py`) with a consumer group; `workers/worker_pool.py`
dispatches through the same load balancer and can run as multiple
independent replicas sharing one queue with zero manual coordination.

Full writeup, including request/context/memory lifecycles, reliability,
observability, security, tradeoffs, and limitations:
[`docs/architecture.md`](docs/architecture.md). Context-specific and
memory-specific design docs: [`docs/context.md`](docs/context.md),
[`docs/memory.md`](docs/memory.md).

## Project structure

```
Fleet-llm-inference/
├── gateway/          # FastAPI app: routes, auth, middleware, metrics
├── router/           # Load balancer (context-aware), circuit breaker, health checker, autoscaler
├── workers/          # Ollama client + Redis Streams-backed worker pool
├── context/          # ContextItem/ContextStore/selection/artifacts/compression
├── memory/           # Working/episodic memory: models, store (Postgres), ranking, retrieval
├── job_queue/        # Redis Streams queue, retry/recovery, dead-letter queue
├── client/           # Thin FleetClient HTTP SDK
├── examples/
│   └── coding_agent/ # Reference agent — real tools, real sandbox, real LLM calls
├── benchmarks/        # REDESIGN.md experiments — real code, real results
│   └── experiments/
├── scripts/           # benchmark.py, simulate.py, smoke_test.py, dev.sh
├── observability/     # Prometheus config + Grafana dashboard
├── config/            # Settings (env-driven)
├── k8s/                # Kubernetes manifests + KEDA ScaledObjects
├── tests/              # 210+ tests — unit, integration (real Redis/Postgres), live-verified
└── docs/                # architecture, context, memory, experiments, migration-plan
```

## Scheduling

Round-robin, least-latency, and queue-depth strategies, all pre-redesign
and unchanged (`ROUTING_STRATEGY`). Context-awareness (added by this
redesign) sits as a *pre-filter* ahead of whichever strategy is active:
a worker whose `max_context_tokens` can't hold the request is excluded
regardless of load; a worker that's merely busy but capable stays
eligible. Demonstrated with real, executed routing decisions (not
assertions) in `docs/experiments.md`'s Experiment 5.

## Worker model

One `OllamaClient` per `WORKER_URLS` entry, uniform
`max_context_tokens` (`WORKER_MAX_CONTEXT_TOKENS`) — this codebase has
no per-worker capability registry, every worker is assumed to run the
same model. vLLM is an explicitly deferred backend (interface stub
only); Ollama is what's actually implemented.

## Reliability

Circuit breaker per worker (unchanged from the original gateway). The
queue was rebuilt on Redis Streams: consumer groups, `XAUTOCLAIM`-based
recovery when a worker crashes mid-job, a dead-letter queue after
`QUEUE_MAX_RETRIES`. Verified live: hard-killed a gateway process
mid-job, confirmed the job was genuinely orphaned in Redis, restarted,
watched the recovery loop rescue it with a real model call. Also
verified live: two independent gateway processes sharing one consumer
group correctly split work with zero duplication — real horizontal
scaling, not just a claim in a doc.

## Agent workload simulator

`scripts/simulate.py` — real HTTP traffic against a real running Fleet
gateway (via `client/fleet_client.py`, the same API a real caller uses),
four workload shapes (`coding`/`research`/`batch`/`mixed`), each
simulated agent keeping its own `agent_id`/`workflow_id` for the
simulation's full duration.

```bash
python scripts/simulate.py --agents 5 --workload mixed --duration 30
```

## Benchmark results

Full experiment writeups with hypothesis/setup/limitations/conclusion:
[`docs/experiments.md`](docs/experiments.md). Three of REDESIGN's six
named experiments are actually runnable today — the other three would
need context selection/artifacts/memory wired into live requests first
(see "what's built-but-not-wired" above); running them anyway would
misrepresent the system rather than measure it, so they weren't faked.

**Experiment 1 — full history vs. budget-aware context** (real
`llama3:latest` call, real task: diagnosing a planted bug):

| Policy | Tokens (actual) | Latency | Task success |
|---|---|---|---|
| full history | 629 | 34.3s | ✓ |
| budget-aware (400-token budget) | 396 (**-39%**) | 22.7s (**-34%**) | ✓ |

**Experiment 5 — context-aware routing**: all 3 tested scenarios
(small/medium/oversized requests against 3 workers with different
capacities) matched REDESIGN.md §41's expected eligibility exactly,
including a heavily-loaded-but-capable worker correctly staying
eligible.

**Experiment 6 — agent bursts** (real gateway, single CPU-only Ollama
worker): 2 and 4 concurrent agents, zero failures at both scales, P50
latency roughly doubled 2→4 agents — consistent with real single-worker
queueing, not a Fleet-side defect. (This environment has one Ollama
instance; results at REDESIGN's 10-500 agent scale need a real
multi-worker fleet this dev setup doesn't have.)

`scripts/benchmark.py` (pre-redesign, still useful for raw
throughput/latency, no context awareness):

```bash
python scripts/benchmark.py -n 50 -c 10
python scripts/benchmark.py -n 50 -c 10 --endpoint /api/v1/queued/generate
```

## Reference coding agent

`examples/coding_agent/` — deliberately simple (REDESIGN.md §28), not an
agent framework: four hardcoded tools (`read_file`/`write_file`/
`search_code`/`run_tests`), a real sandboxed toy repo with a planted bug,
a fixed six-step sequence where three steps are genuine LLM calls through
Fleet and three are real tool calls. Run it against a live gateway:

```bash
python examples/coding_agent/agent.py
```

The code fix itself is scripted rather than parsed from the model's
freeform response — see the module docstring for why that's a deliberate
scope decision about what this demo is for, not a limitation of Fleet.

## Getting started

Application code is plain Python (FastAPI/uvicorn/asyncpg/httpx, all with
proper native Windows wheels) and has no POSIX-only calls — the only
OS-specific things in this repo are the *infrastructure* dependencies
(Redis has no official native Windows server) and the `scripts/*.sh`
convenience scripts (bash). See "Windows" below for the practical
consequence of that split.

### Local (host) — Linux / macOS / WSL

```bash
cp .env.example .env            # then edit values

# dependencies running on the host
ollama serve &                  # worker on :11434  (ollama pull llama3)
redis-server &                  # queue
# PostgreSQL, for memory storage — native install, not a container here:
sudo apt-get install postgresql
sudo -u postgres createuser -s $(whoami)
sudo -u postgres createdb -O $(whoami) fleet

pip install -r gateway/requirements.txt
uvicorn gateway.main:app --reload
```

Smoke test (includes the queued path, exercising the real Redis Streams
queue):

```bash
python scripts/smoke_test.py --model llama3:latest
```

### Docker Compose

```bash
docker compose up --build                              # dev (hot reload)
docker compose -f docker-compose.prod.yml up -d --build   # single-machine prod
```

- Gateway: http://localhost:8000 (`/docs`, `/metrics`)
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin / admin) — dashboard now includes
  an "Agent / Context" row alongside the original request/worker panels.
- `docker-compose.yml` now includes a `postgres` service for memory
  storage, and both compose files correctly `COPY`/bind-mount
  `context/`, `memory/`, and `job_queue/` (the packages this redesign
  added) into the gateway image/container.
- GPU: `worker1` reserves an NVIDIA GPU by default. Confirm passthrough
  first with `docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi`;
  if that fails, delete `worker1`'s `deploy:` block to fall back to
  CPU-only (`worker2` already has no GPU reservation).

### Windows

**Native Windows (no WSL, no Docker) is not recommended — Redis is the
blocker.** Redis Inc. doesn't ship an official native Windows server
binary; `redis-server` is a Linux/macOS thing. Everything else (Ollama,
Python, FastAPI, uvicorn, asyncpg) has proper native Windows support, and
`config/settings.py`'s only OS-flavored default
(`MEMORY_DB_HOST`, which also accepts a plain TCP hostname) is just a
config value, not a hard dependency — but without a working Redis you
can't bring the gateway up at all, since the queue connects at startup.

**Use Docker Compose instead — it's the recommended path on native
Windows** (PowerShell, Docker Desktop with the WSL2 backend):

```powershell
Copy-Item .env.example .env     # then edit values
docker compose up --build
```

Docker Desktop runs Linux containers regardless of the Windows host, so
Redis/Postgres/Ollama all come up exactly as they do on Linux — nothing
in the compose files is Windows-specific because nothing needs to be.
Same GPU preflight command as above works from PowerShell unchanged.
**Honesty check**: the Dockerfile/compose-file bugs (missing `COPY`s,
missing `depends_on: condition: service_healthy`, missing prod Postgres
service) were found and fixed by code review, not by an actual container
build — Docker has not been available in any environment this project
was developed in until now. The smoke test below is what actually proves
the fix works, on your machine, for real.

The three paths, concretely:

| Path | Works on native Windows? | Notes |
|---|---|---|
| `docker compose up --build` | Should — fixed, not yet run live | Recommended. Needs Docker Desktop + WSL2 backend. |
| Native host processes (`### Local (host)` above) | No, blocked on Redis | Would need a Redis substitute (e.g. Memurai) — untested here. |
| WSL2 (Ubuntu) + the Linux host path | Yes, actually run end-to-end | What this project was developed and verified against; not "native" Windows but no Docker required. |

See "Test case: smoke test on Windows" below to validate the Docker
Compose stack end-to-end once it's up — that run is what turns "should
work" into "does work."

#### Test case: smoke test on Windows

Validates the whole stack — gateway, Redis Streams queue, Ollama worker,
Postgres — from a native Windows shell, using the same
`scripts/smoke_test.py` the Linux/WSL path uses.

```powershell
# 1. Bring the stack up (first build pulls several images — can take a while)
docker compose up --build -d
docker compose ps                      # all services should show "healthy" or "running"

# 2. Set DEFAULT_MODEL in .env to a real model tag, THEN pull that same
#    model into BOTH worker containers — not just one. Fleet load-balances
#    across every worker in WORKER_URLS; a worker that's missing the model
#    doesn't fail closed, it 404s per-request. The circuit breaker opens
#    after a few of those, but retries it (HALF_OPEN) every
#    CIRCUIT_BREAKER_TIMEOUT seconds, so a mismatched worker causes
#    intermittent, not permanent, 503s on /generate and /chat — confusing
#    to debug if you don't know both workers need the pull.
docker compose exec worker1 ollama pull llama3.1:8b
docker compose exec worker2 ollama pull llama3.1:8b

# 3. Run the smoke test from the Windows host (containers publish to localhost)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install httpx
python scripts\smoke_test.py --skip-inference          # fast pass: wiring/auth/health
python scripts\smoke_test.py --model llama3.1:8b       # full pass: real /generate, /chat, /queued/generate calls
```

Expected fast-pass output (`--skip-inference`):

```
[PASS] GET /api/v1/health — status=200
[PASS] POST /api/v1/generate rejects missing api key — status=401
[PASS] GET /api/v1/workers rejects bad api key — status=401
[PASS] GET /api/v1/workers — status=200
[PASS] GET /api/v1/queue/depth — status=200
[PASS] GET /metrics — status=200

5/5 checks passed
```

Exit code is `0` on success, `1` otherwise. `--api-key`/`--model` must
match your `.env`'s `API_KEY`/`DEFAULT_MODEL` — the smoke test's own
defaults (`dev-key` / `llama3:latest`) are just fallbacks for when you
haven't overridden them, not a claim that `llama3:latest` is what's
actually pulled; verify with `docker compose exec worker1 ollama list`
(and `worker2`) before assuming a 503 is a real bug rather than a missing
model. Tear down with `docker compose down` (add `-v` to also drop the
Redis/Postgres/Ollama volumes).

### Kubernetes + KEDA

`k8s/` holds the manifest set (applied in name order) — **note:
`k8s/` was not updated with a Postgres deployment when memory storage
was added**, so the Kubernetes path currently only supports the
inference-gateway portion of Fleet, not durable memory. Fixing that is
listed in `docs/architecture.md`'s Future Work.

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --namespace keda --create-namespace

docker build -f gateway/Dockerfile -t ghcr.io/<you>/llm-gateway:latest .
docker push ghcr.io/<you>/llm-gateway:latest
# replace REPLACE_ME in k8s/30-gateway.yaml with that image

kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/
kubectl -n llm-engine get pods -w

kubectl -n llm-engine create secret generic gateway-secrets \
  --from-literal=API_KEY="$(openssl rand -hex 16)" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n llm-engine port-forward svc/gateway 8000:80
```

Full walkthrough in [`k8s/README.md`](k8s/README.md); cloud paths in
[`docs/deployment.md`](docs/deployment.md).

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/generate` | Single prompt. Accepts optional `agent_id`/`workflow_id`/`request_id`/`parent_request_id`. |
| `POST` | `/api/v1/chat` | Multi-message chat completion. Same optional agent metadata. |
| `POST` | `/api/v1/queued/generate` | Enqueue via Redis Streams, await result. |
| `GET`  | `/api/v1/health` | Status, queue depth, worker stats. |
| `GET`  | `/api/v1/workers` | Per-worker health / latency / circuit state. |
| `GET`  | `/api/v1/queue/depth` | Current queue depth. |
| `GET`  | `/metrics` | Prometheus metrics (`llm_*` request/worker metrics, `fleet_*` agent/context metrics). |

All `POST` routes require the `x-api-key` header. Example, agent-aware:

```bash
curl -s -X POST localhost:8000/api/v1/generate \
  -H "x-api-key: dev-key" -H 'content-type: application/json' \
  -d '{"prompt":"In one sentence, what is a load balancer?","agent_id":"demo-agent","workflow_id":"demo-wf-1"}'
```

## Configuration

All config is plain `os.getenv` in `config/settings.py`, documented in
`.env.example`. Notably: `WORKER_MAX_CONTEXT_TOKENS`,
`CONTEXT_BUDGET_DEFAULT`/`CONTEXT_SELECTION_POLICY` (defined, currently
unread by any live code — see status section above), `MEMORY_DB_*`
(Postgres), `QUEUE_*` (Redis Streams retry/recovery tuning).

## What Fleet is not

Per REDESIGN.md §80/§81: Fleet does not implement KV-cache orchestration
or GPU cache scheduling — nothing in this codebase touches a model's
KV-cache, and it doesn't claim to. It does not inspect or manipulate a
model's hidden reasoning — it manages task state, context, tool outputs,
and memory metadata, not what a model "thinks." It is not an agent
framework — the reference agent in `examples/` is deliberately simple
and hardcoded, not extensible by design (see its module docstring).

## Design decisions and limitations

See `docs/architecture.md` §13-14 for the full list — including why
`job_queue/` isn't named `queue/` (a real Python stdlib collision,
caught before it shipped), why context metrics aren't labeled per-agent
(unbounded cardinality), and what horizontal scaling has and hasn't
actually been verified.

## Future work

`vLLM` support, semantic memory (embeddings/vector search), the full
`FleetAgent` SDK wrapper, and cost-based budgets are explicitly out of
scope for this redesign (REDESIGN.md §0.2), not partially built. The
single largest next step is wiring context selection, artifacts,
compression, and memory retrieval into the live request path — full list
in `docs/architecture.md`'s Future Work section.
