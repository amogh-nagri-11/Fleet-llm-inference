# Kubernetes deployment

Manifests for running the LLM Inference Engine on Kubernetes with KEDA-driven
autoscaling on Redis queue depth.

## Layout

| File | Resource |
|---|---|
| `00-namespace.yaml` | `llm-engine` namespace |
| `01-configmap.yaml` | gateway config + API key secret |
| `10-redis.yaml` | Redis Deployment + Service (the queue) |
| `20-ollama.yaml` | Ollama worker Deployment + Service (scaled by KEDA) |
| `30-gateway.yaml` | Gateway Deployment + Service |
| `40-keda-scaledobject.yaml` | KEDA ScaledObjects (Ollama + gateway) |

## Prerequisites

1. A cluster (GKE/EKS/AKS, or k3s/kind for local). GPU nodes recommended for
   real inference throughput.
2. KEDA installed:
   ```bash
   helm repo add kedacore https://kedacore.github.io/charts
   helm install keda kedacore/keda --namespace keda --create-namespace
   ```
3. The gateway image pushed to a registry your cluster can pull. Build from the
   repo root (the image bundles the top-level `config`/`router`/`workers`):
   ```bash
   docker build -f gateway/Dockerfile -t ghcr.io/<you>/llm-gateway:latest .
   docker push ghcr.io/<you>/llm-gateway:latest
   ```
   Then set that image in `30-gateway.yaml` (replace `REPLACE_ME`).

## Deploy

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/                     # applies the rest in name order
kubectl -n llm-engine get pods -w
```

Update the API key before exposing anything:

```bash
kubectl -n llm-engine create secret generic gateway-secrets \
  --from-literal=API_KEY="$(openssl rand -hex 16)" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Port-forward to test:

```bash
kubectl -n llm-engine port-forward svc/gateway 8000:80
curl -s -X POST localhost:8000/api/v1/generate \
  -H "x-api-key: <your-key>" -H 'content-type: application/json' \
  -d '{"prompt":"hello"}'
```

## How autoscaling works here

```
client → gateway (/queued/generate) → RPUSH llm:request_queue (Redis)
                                          │
              KEDA reads LLEN every 15s ──┤
                                          ▼
           desiredReplicas = ceil(LLEN / listLength)   (listLength = 3)
                                          │
                    ┌─────────────────────┴─────────────────────┐
                    ▼                                            ▼
        scale Ollama Deployment                        scale gateway Deployment
        (more inference capacity)                      (more queue consumers)
```

The queue **consumer** is the gateway's `WorkerPool.blpop` loop, so each gateway
replica drains the queue serially. Scaling Ollama alone raises raw inference
capacity but not consumer parallelism — that's why `40-keda-scaledobject.yaml`
also scales the gateway. Drop the `gateway-queue-scaler` object if you'd rather
keep a fixed gateway replica count.

> The in-app autoscaler (`router/autoscaler.py`) is for non-Kubernetes hosts
> (bare Docker or WSL). In the cluster it's effectively disabled via the
> ConfigMap (`STANDBY_WORKER_URLS=""`), and KEDA owns scaling instead.
