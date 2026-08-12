import os
from dotenv import load_dotenv

load_dotenv()

# ── Gateway ────────────────────────────────────────────
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", 8000))
API_KEY      = os.getenv("API_KEY", "dev-key")

# ── Workers ────────────────────────────────────────────
_raw_urls   = os.getenv("WORKER_URLS", "http://localhost:11434")
WORKER_URLS = [u.strip() for u in _raw_urls.split(",") if u.strip()]

# ── Model ──────────────────────────────────────────────
DEFAULT_MODEL   = os.getenv("DEFAULT_MODEL", "llama3:latest")
MAX_TOKENS      = int(os.getenv("MAX_TOKENS", 2048))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 120))

# ── Routing ────────────────────────────────────────────
ROUTING_STRATEGY = os.getenv("ROUTING_STRATEGY", "round_robin")

# ── Context budgeting ──────────────────────────────────
# Default max input tokens the context selector packs a workflow's context
# down to (REDESIGN.md §9). Wired into the live request path as of Phase 9.
CONTEXT_BUDGET_DEFAULT   = int(os.getenv("CONTEXT_BUDGET_DEFAULT", 8192))
CONTEXT_SELECTION_POLICY = os.getenv("CONTEXT_SELECTION_POLICY", "hybrid")

# ── Context compression (REDESIGN.md §13) ──────────────
# Once a workflow's total recorded context exceeds this many tokens, the
# oldest CONVERSATION turns get summarized into one SUMMARY item in the
# background. Defaults to CONTEXT_BUDGET_DEFAULT so compression kicks in
# right around the same point the selector would otherwise start discarding
# old turns outright — compression trades them for a summary instead.
CONTEXT_COMPRESSION_THRESHOLD   = int(os.getenv("CONTEXT_COMPRESSION_THRESHOLD", CONTEXT_BUDGET_DEFAULT))
# Most-recent conversation turns left untouched even when over budget, so a
# compression pass never eats the exchange that just happened.
CONTEXT_COMPRESSION_KEEP_RECENT = int(os.getenv("CONTEXT_COMPRESSION_KEEP_RECENT", 4))

# Worker context capacity for context-aware routing (REDESIGN.md §24/§41).
# Uniform across workers for now — this codebase doesn't track per-worker
# model/capability differences (that's the superseded draft's worker
# registry concept, not part of this document's scope). 8192 matches
# llama3:8b's actual context_length.
WORKER_MAX_CONTEXT_TOKENS = int(os.getenv("WORKER_MAX_CONTEXT_TOKENS", 8192))

# ── Autoscaling ────────────────────────────────────────
# Standby Ollama URLs the autoscaler registers into the load balancer at
# runtime when Docker container spin-up is unavailable (e.g. WSL without a
# Docker daemon). Comma-separated. These instances must already be running.
_raw_standby        = os.getenv("STANDBY_WORKER_URLS", "")
STANDBY_WORKER_URLS = [u.strip() for u in _raw_standby.split(",") if u.strip()]

AUTOSCALE_MIN_WORKERS  = int(os.getenv("AUTOSCALE_MIN_WORKERS", len(WORKER_URLS) or 1))
AUTOSCALE_MAX_WORKERS  = int(os.getenv("AUTOSCALE_MAX_WORKERS", 4))
AUTOSCALE_UP_THRESHOLD   = int(os.getenv("AUTOSCALE_UP_THRESHOLD", 3))
AUTOSCALE_DOWN_THRESHOLD = int(os.getenv("AUTOSCALE_DOWN_THRESHOLD", 0))
AUTOSCALE_CHECK_INTERVAL = int(os.getenv("AUTOSCALE_CHECK_INTERVAL", 10))
AUTOSCALE_COOLDOWN       = int(os.getenv("AUTOSCALE_COOLDOWN", 30))

# ── Redis ──────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB   = int(os.getenv("REDIS_DB", 0))

# ── Memory storage (PostgreSQL, REDESIGN.md §18) ───────
# MEMORY_DB_HOST can be a hostname (TCP) or a unix socket directory (e.g.
# /var/run/postgresql) for local peer-auth dev — asyncpg supports both via
# the same `host` kwarg. Durable storage for working/episodic memory only;
# semantic memory / vector search stays out of scope (§0.2).
MEMORY_DB_HOST     = os.getenv("MEMORY_DB_HOST", "localhost")
MEMORY_DB_PORT     = int(os.getenv("MEMORY_DB_PORT", 5432))
MEMORY_DB_NAME     = os.getenv("MEMORY_DB_NAME", "fleet")
MEMORY_DB_USER     = os.getenv("MEMORY_DB_USER", "fleet")
MEMORY_DB_PASSWORD = os.getenv("MEMORY_DB_PASSWORD", "")

# ── Circuit Breaker ────────────────────────────────────
CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", 5))
CIRCUIT_BREAKER_TIMEOUT   = int(os.getenv("CIRCUIT_BREAKER_TIMEOUT", 30))

# ── Reliable queue (Redis Streams, REDESIGN.md §43) ────
QUEUE_CONSUMER_GROUP           = os.getenv("QUEUE_CONSUMER_GROUP", "fleet-workers")
QUEUE_MAX_RETRIES              = int(os.getenv("QUEUE_MAX_RETRIES", 3))
QUEUE_PENDING_MIN_IDLE_MS      = int(os.getenv("QUEUE_PENDING_MIN_IDLE_MS", 30000))
QUEUE_RECOVERY_INTERVAL_SECONDS = int(os.getenv("QUEUE_RECOVERY_INTERVAL_SECONDS", 15))

# ── Observability ──────────────────────────────────────
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", 9090))
GRAFANA_PORT    = int(os.getenv("GRAFANA_PORT", 3000))