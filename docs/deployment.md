# Deployment guide

Three ways to run the engine, from simplest to most scalable.

## 1. Local / dev (host)

Ollama + Redis on the host, gateway via uvicorn:

```bash
ollama serve &                          # worker on :11434
redis-server &                          # queue
cp .env.example .env                    # then edit values
pip install -r gateway/requirements.txt
uvicorn gateway.main:app --reload
```

On a host without a Docker daemon (e.g. WSL), the in-app autoscaler falls back
to **static worker registration**: start extra Ollama instances and list them in
`STANDBY_WORKER_URLS`. The autoscaler registers them into the load balancer when
the queue is deep and deregisters them when it drains:

```bash
OLLAMA_HOST=0.0.0.0:11435 ollama serve &      # a second worker
export STANDBY_WORKER_URLS=http://localhost:11435
uvicorn gateway.main:app
```

## 2. Single machine (Docker Compose)

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Brings up the gateway, Redis (AOF persistence), two GPU Ollama workers,
Prometheus, and Grafana (dashboard auto-provisioned at http://localhost:3000,
admin / `$GRAFANA_PASSWORD`). Drop the `deploy.resources` GPU blocks for a
CPU-only host.

## 3. Kubernetes + KEDA

See [`k8s/README.md`](../k8s/README.md). KEDA autoscales the Ollama workers and
gateway on Redis queue depth (`llm:request_queue`).

---

## Cloud platforms

### GPU workers — RunPod

RunPod GPU pods run the Ollama image directly:

- **Image:** `ollama/ollama:latest`
- **Expose** HTTP port `11434`.
- **Volume:** mount a network volume at `/root/.ollama` so pulled models survive
  restarts.
- After the pod is up: `ollama pull llama3:latest`.
- Put the pod's public URL in the gateway's `WORKER_URLS` (or
  `STANDBY_WORKER_URLS` for autoscaled standby capacity).

Use RunPod for the *workers* and run the gateway somewhere cheap (below) — the
gateway is CPU/IO-bound and doesn't need a GPU.

### Gateway — Railway or Fly.io

The gateway is a stateless FastAPI container; point it at managed Redis and your
GPU worker URLs.

**Railway**
- New service → Deploy from repo. Set the Dockerfile path to `gateway/Dockerfile`
  and the **build context / root to the repo root** (the image needs the
  top-level packages).
- Add the Railway Redis plugin; set `REDIS_HOST`/`REDIS_PORT` from it.
- Set `WORKER_URLS` to your RunPod worker URL(s), and `API_KEY`.

**Fly.io**
```bash
fly launch --dockerfile gateway/Dockerfile --no-deploy
fly redis create                         # or attach Upstash
fly secrets set API_KEY=... WORKER_URLS=https://<runpod-host> REDIS_HOST=... REDIS_PORT=...
fly deploy
```
`fly.toml` should set `internal_port = 8000` and a health check on `/`.

> Whichever platform builds the gateway image, the **build context must be the
> repository root**, not `gateway/` — the Dockerfile copies `config/`, `router/`
> and `workers/` from the root.
