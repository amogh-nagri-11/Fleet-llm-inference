import asyncio
import time
import redis.asyncio as aioredis

from config import settings
from router.load_balancer import load_balancer
from gateway.metrics import QUEUE_DEPTH, WORKER_COUNT

# Docker is optional. In environments without the Docker SDK (or without a
# running daemon, e.g. WSL with Docker Desktop integration off) the autoscaler
# transparently falls back to registering pre-configured standby Ollama URLs
# into the load balancer at runtime.
try:
    import docker
except ImportError:  # pragma: no cover - depends on environment
    docker = None


class Autoscaler:
    def __init__(
        self,
        min_workers: int = 1,
        max_workers: int = 4,
        scale_up_threshold: int = 3,    # queue depth to trigger scale up
        scale_down_threshold: int = 0,  # queue depth to trigger scale down
        check_interval: int = 10,       # seconds between checks
        cooldown: int = 30,             # seconds between scaling actions
        standby_urls: list[str] | None = None,
    ):
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.scale_up_threshold = scale_up_threshold
        self.scale_down_threshold = scale_down_threshold
        self.check_interval = check_interval
        self.cooldown = cooldown

        self.redis = None
        self.docker_client = None
        self._task = None
        self._last_scale_time = 0.0
        self._worker_containers: list[str] = []  # docker container IDs we've spawned

        # Static-fallback state
        self.standby: list[str] = list(standby_urls or [])  # URLs available to register
        self.active_dynamic: list[str] = []                 # URLs we've registered at runtime

        self.queue_key = "llm:request_queue"

    @property
    def mode(self) -> str:
        return "docker" if self.docker_client else "static"

    async def connect(self):
        self.redis = await aioredis.from_url(
            f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
            decode_responses=True,
        )
        if docker is not None:
            try:
                self.docker_client = docker.from_env()
                self.docker_client.ping()
                print("[Autoscaler] Connected to Docker — container scaling enabled")
            except Exception as e:
                self.docker_client = None
                print(f"[Autoscaler] Docker unavailable ({e}); "
                      f"using static fallback with {len(self.standby)} standby worker(s)")
        else:
            print(f"[Autoscaler] Docker SDK not installed; "
                  f"using static fallback with {len(self.standby)} standby worker(s)")
        print(f"[Autoscaler] Connected to Redis | mode={self.mode}")

    async def _queue_depth(self) -> int:
        return await self.redis.llen(self.queue_key)

    def _running_worker_count(self) -> int:
        if self.docker_client:
            try:
                containers = self.docker_client.containers.list(
                    filters={"name": "llm-worker-dynamic"}
                )
                return len(containers) + len(settings.WORKER_URLS)
            except Exception:
                return len(settings.WORKER_URLS)
        # Static mode: whatever is currently registered in the load balancer.
        return len(load_balancer.workers)

    def _in_cooldown(self) -> bool:
        return (time.monotonic() - self._last_scale_time) < self.cooldown

    # ── Scale up ──────────────────────────────────────────────────────────
    async def _scale_up(self):
        current = self._running_worker_count()
        if current >= self.max_workers:
            print(f"[Autoscaler] Already at max workers ({self.max_workers}), skipping scale up")
            return
        if self.docker_client:
            await self._scale_up_docker(current)
        else:
            await self._scale_up_static()

    async def _scale_up_docker(self, current: int):
        print(f"[Autoscaler] Scaling UP (docker) — spawning worker {current + 1}")
        try:
            port = 11434 + current
            container = self.docker_client.containers.run(
                "ollama/ollama:latest",
                detach=True,
                name=f"llm-worker-dynamic-{port}",
                ports={"11434/tcp": port},
                volumes={"ollama_data": {"bind": "/root/.ollama", "mode": "rw"}},
            )
            self._worker_containers.append(container.id)
            self._last_scale_time = time.monotonic()
            # Register the freshly-started container with the load balancer too.
            load_balancer.add_worker(f"http://localhost:{port}")
            print(f"[Autoscaler] Worker spawned on port {port} | container={container.short_id}")
        except Exception as e:
            print(f"[Autoscaler] Failed to scale up via docker: {e}")

    async def _scale_up_static(self):
        if not self.standby:
            print("[Autoscaler] Scale up requested but no standby workers available to register")
            return
        url = self.standby.pop(0)
        if not load_balancer.add_worker(url):
            print(f"[Autoscaler] Worker {url} already registered, skipping")
            return
        # Health-check the newly registered worker so the LB routes to it only if up.
        client = next((w for w in load_balancer.workers if w.stats.url == url), None)
        healthy = await client.health_check() if client else False
        self.active_dynamic.append(url)
        self._last_scale_time = time.monotonic()
        print(f"[Autoscaler] Scaled UP (static) — registered {url} "
              f"(healthy={healthy}) | total workers={self._running_worker_count()}")

    # ── Scale down ────────────────────────────────────────────────────────
    async def _scale_down(self):
        current = self._running_worker_count()
        if current <= self.min_workers:
            print(f"[Autoscaler] Already at min workers ({self.min_workers}), skipping scale down")
            return
        if self.docker_client:
            await self._scale_down_docker()
        else:
            await self._scale_down_static()

    async def _scale_down_docker(self):
        if not self._worker_containers:
            return
        container_id = self._worker_containers.pop()
        print(f"[Autoscaler] Scaling DOWN (docker) — removing container {container_id[:12]}")
        try:
            container = self.docker_client.containers.get(container_id)
            container.stop(timeout=10)
            container.remove()
            self._last_scale_time = time.monotonic()
            print("[Autoscaler] Worker removed")
        except Exception as e:
            print(f"[Autoscaler] Failed to scale down via docker: {e}")

    async def _scale_down_static(self):
        if not self.active_dynamic:
            return
        url = self.active_dynamic.pop()
        load_balancer.remove_worker(url)
        self.standby.insert(0, url)  # return it to the pool for reuse
        self._last_scale_time = time.monotonic()
        print(f"[Autoscaler] Scaled DOWN (static) — deregistered {url} "
              f"| total workers={self._running_worker_count()}")

    # ── Loop ──────────────────────────────────────────────────────────────
    async def _run(self):
        print("[Autoscaler] Monitoring loop started")
        while True:
            try:
                await asyncio.sleep(self.check_interval)

                depth = await self._queue_depth()
                workers = self._running_worker_count()
                QUEUE_DEPTH.set(depth)
                WORKER_COUNT.set(workers)
                print(f"[Autoscaler] queue_depth={depth} | workers={workers} | mode={self.mode}")

                if self._in_cooldown():
                    continue

                if depth >= self.scale_up_threshold:
                    await self._scale_up()
                elif depth <= self.scale_down_threshold and workers > self.min_workers:
                    await self._scale_down()

            except Exception as e:
                print(f"[Autoscaler] Error: {e}")
                await asyncio.sleep(5)

    def start(self):
        self._task = asyncio.create_task(self._run())
        print("[Autoscaler] Started")

    def stop(self):
        if self._task:
            self._task.cancel()


autoscaler = Autoscaler(
    min_workers=settings.AUTOSCALE_MIN_WORKERS,
    max_workers=settings.AUTOSCALE_MAX_WORKERS,
    scale_up_threshold=settings.AUTOSCALE_UP_THRESHOLD,
    scale_down_threshold=settings.AUTOSCALE_DOWN_THRESHOLD,
    check_interval=settings.AUTOSCALE_CHECK_INTERVAL,
    cooldown=settings.AUTOSCALE_COOLDOWN,
    standby_urls=settings.STANDBY_WORKER_URLS,
)
