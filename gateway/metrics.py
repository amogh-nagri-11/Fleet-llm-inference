from prometheus_client import Counter, Histogram, Gauge

# ── Request-level metrics ─────────────────────────────────
REQUEST_COUNT = Counter(
    "llm_requests_total",
    "Total number of requests",
    ["method", "endpoint", "status_code"]
)

REQUEST_LATENCY = Histogram(
    "llm_request_latency_seconds",
    "Request latency in seconds",
    ["endpoint"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
)

ACTIVE_REQUESTS = Gauge(
    "llm_active_requests",
    "Number of requests currently being processed"
)

QUEUE_DEPTH = Gauge(
    "llm_queue_depth",
    "Pending jobs in the Redis request queue"
)

WORKER_COUNT = Gauge(
    "llm_worker_count",
    "Number of workers currently registered with the load balancer"
)

# ── Worker-level metrics ──────────────────────────────────
WORKER_ACTIVE_REQUESTS = Gauge(
    "llm_worker_active_requests",
    "Active requests per worker",
    ["worker_url"]
)

WORKER_REQUEST_COUNT = Counter(
    "llm_worker_requests_total",
    "Total requests per worker",
    ["worker_url", "status"]
)

WORKER_LATENCY = Histogram(
    "llm_worker_latency_seconds",
    "Per-worker response latency",
    ["worker_url"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
)

WORKER_HEALTH = Gauge(
    "llm_worker_healthy",
    "Worker health status (1=healthy, 0=unhealthy)",
    ["worker_url"]
)