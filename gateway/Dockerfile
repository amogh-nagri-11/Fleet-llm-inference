# syntax=docker/dockerfile:1
#
# Build from the REPOSITORY ROOT, not from ./gateway, because the gateway
# imports the top-level `config`, `router` and `workers` packages:
#
#   docker build -f gateway/Dockerfile -t llm-gateway .
#
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY gateway/requirements.txt ./gateway/requirements.txt
RUN pip install --no-cache-dir -r gateway/requirements.txt

# Application code (only what the gateway needs at runtime).
COPY config ./config
COPY router ./router
COPY workers ./workers
COPY gateway ./gateway

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/').status==200 else 1)"

# Production: no --reload. Scale horizontally via replicas, not uvicorn workers,
# so each replica keeps a single WorkerPool consumer loop.
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
