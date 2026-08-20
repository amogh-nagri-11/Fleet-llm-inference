import json
from typing import List, Optional

import redis.asyncio as aioredis

from config import settings
from registry.models import WorkerRecord

WORKER_KEY_PREFIX = "fleet:registry:worker:"
WORKERS_SET_KEY = "fleet:registry:workers"


class RegistryStore:
    """Redis-backed registry of dynamically-joining worker machines.
    Separate Redis connection from worker_pool/autoscaler, matching this
    codebase's existing convention (each component opens its own client
    against settings.REDIS_HOST/PORT/DB rather than sharing one).

    Each worker is a JSON blob at fleet:registry:worker:{id} (same
    JSON-string-value pattern job_queue/worker_pool.py uses for results),
    plus a Set of ids for O(1) enumeration without a KEYS scan.
    """

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        self.redis = await aioredis.from_url(
            f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
            decode_responses=True,
        )

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()
            self.redis = None

    def _key(self, worker_id: str) -> str:
        return f"{WORKER_KEY_PREFIX}{worker_id}"

    async def register(self, record: WorkerRecord) -> WorkerRecord:
        """Upsert. A machine re-registering (reboot, or a future periodic
        heartbeat reusing this same call) keeps its original
        registered_at instead of looking like a brand-new join every
        time — only last_heartbeat and the profile fields move."""
        existing = await self.get_worker(record.worker_id)
        if existing is not None:
            record.registered_at = existing.registered_at

        await self.redis.set(self._key(record.worker_id), json.dumps(record.to_dict()))
        await self.redis.sadd(WORKERS_SET_KEY, record.worker_id)
        return record

    async def get_worker(self, worker_id: str) -> Optional[WorkerRecord]:
        raw = await self.redis.get(self._key(worker_id))
        if raw is None:
            return None
        return WorkerRecord.from_dict(json.loads(raw))

    async def list_workers(self) -> List[WorkerRecord]:
        ids = await self.redis.smembers(WORKERS_SET_KEY)
        if not ids:
            return []

        raw_values = await self.redis.mget([self._key(wid) for wid in ids])
        workers = []
        for worker_id, raw in zip(ids, raw_values):
            if raw is None:
                # Set points at a key that's gone (deregistered by another
                # path, or manually deleted) — drop the ghost membership
                # instead of returning a hole.
                await self.redis.srem(WORKERS_SET_KEY, worker_id)
                continue
            workers.append(WorkerRecord.from_dict(json.loads(raw)))
        return workers

    async def deregister(self, worker_id: str) -> bool:
        removed = await self.redis.delete(self._key(worker_id))
        await self.redis.srem(WORKERS_SET_KEY, worker_id)
        return removed > 0


registry_store = RegistryStore()
