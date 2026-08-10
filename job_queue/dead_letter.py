import json
import time
from typing import Optional

import redis.asyncio as aioredis


class DeadLetterQueue:
    """Storage for jobs that exhausted their retry budget (REDESIGN.md
    §17). A separate stream, not a silent drop — dead-lettered jobs stay
    inspectable."""

    def __init__(self, key: str = "llm:stream:dead_letter"):
        self.key = key
        self.redis: Optional[aioredis.Redis] = None

    async def connect(self, redis_client: aioredis.Redis) -> None:
        self.redis = redis_client

    async def add(self, original_entry_id: str, payload: dict, reason: str) -> str:
        fields = {
            "original_entry_id": original_entry_id,
            "reason": reason,
            "failed_at": str(time.time()),
            "payload": json.dumps(payload),
        }
        return await self.redis.xadd(self.key, fields)

    async def list_all(self, count: int = 100) -> list[dict]:
        entries = await self.redis.xrange(self.key, count=count)
        return [
            {
                "entry_id": entry_id,
                "original_entry_id": fields["original_entry_id"],
                "reason": fields["reason"],
                "failed_at": float(fields["failed_at"]),
                "payload": json.loads(fields["payload"]),
            }
            for entry_id, fields in entries
        ]

    async def depth(self) -> int:
        return await self.redis.xlen(self.key)
