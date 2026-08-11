import json
import uuid
from typing import Optional

import redis.asyncio as aioredis


def encode_fields(payload: dict) -> dict:
    return {k: json.dumps(v) for k, v in payload.items()}


def decode_fields(fields: dict) -> dict:
    return {k: json.loads(v) for k, v in fields.items()}


class RedisStreamQueue:
    """Reliable job queue on Redis Streams with a consumer group
    (REDESIGN.md §43/§72 Phase 10). Replaces the RPUSH/BLPOP queue
    `workers/worker_pool.py` used through Phase 9 — that had no
    acknowledgement, no recovery when a worker crashed mid-job, and no
    dead-letter handling (flagged as a Phase 1 migration item).

    Named `job_queue/`, not the REDESIGN.md §71 literal `queue/` — that
    name shadows Python's stdlib `queue` module for every dependency that
    does `import queue` internally (uvicorn/starlette/anyio all do),
    since the repo root has to stay on sys.path for `gateway.main` etc.
    to import. Confirmed the collision before choosing this name.
    """

    def __init__(
        self,
        stream_key: str = "llm:stream:requests",
        group_name: str = "fleet-workers",
        consumer_name: Optional[str] = None,
    ):
        self.stream_key = stream_key
        self.group_name = group_name
        self.consumer_name = consumer_name or f"consumer-{uuid.uuid4().hex[:8]}"
        self.redis: Optional[aioredis.Redis] = None

    async def connect(self, redis_client: aioredis.Redis) -> None:
        self.redis = redis_client
        try:
            await self.redis.xgroup_create(
                self.stream_key, self.group_name, id="0", mkstream=True
            )
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise  # group already exists — fine, this is idempotent setup

    async def enqueue(self, payload: dict) -> str:
        return await self.redis.xadd(self.stream_key, encode_fields(payload))

    async def read(self, count: int = 1, block_ms: int = 1000) -> list[tuple[str, dict]]:
        """XREADGROUP for this consumer — only ever sees new (`>`) entries,
        never entries already claimed by another consumer."""
        resp = await self.redis.xreadgroup(
            self.group_name, self.consumer_name,
            {self.stream_key: ">"}, count=count, block=block_ms,
        )
        if not resp:
            return []
        _, entries = resp[0]
        return [(entry_id, decode_fields(fields)) for entry_id, fields in entries]

    async def ack(self, entry_id: str) -> None:
        await self.redis.xack(self.stream_key, self.group_name, entry_id)

    async def depth(self) -> int:
        return await self.redis.xlen(self.stream_key)
