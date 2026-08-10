from dataclasses import dataclass

from job_queue.dead_letter import DeadLetterQueue
from job_queue.streams import RedisStreamQueue, decode_fields


@dataclass
class RecoveryResult:
    retryable: list[tuple[str, dict]]      # (entry_id, payload) to reprocess
    dead_lettered: list[str]                # entry_ids moved to the DLQ


class RetryPolicy:
    """Recovers unacknowledged jobs (REDESIGN.md §17) — a worker that
    crashed mid-job leaves its entry in the stream's Pending Entries List
    (PEL). After `min_idle_ms` with no ACK, XAUTOCLAIM hands it to another
    consumer to retry; past `max_retries` it goes to the dead-letter queue
    instead.

    Retry count comes from Redis' own per-entry delivery counter
    (XPENDING's `times_delivered`), not an in-process dict — that counter
    survives process restarts and is shared across every consumer, unlike
    state a single WorkerPool instance would track itself.
    """

    def __init__(self, queue: RedisStreamQueue, max_retries: int = 3, min_idle_ms: int = 30000):
        self.queue = queue
        self.max_retries = max_retries
        self.min_idle_ms = min_idle_ms

    async def _delivery_count(self, entry_id: str) -> int:
        redis = self.queue.redis
        entries = await redis.xpending_range(
            self.queue.stream_key, self.queue.group_name,
            min=entry_id, max=entry_id, count=1,
        )
        if not entries:
            return 0
        return entries[0]["times_delivered"]

    async def _reclaim_stale(self) -> list[tuple[str, dict]]:
        redis = self.queue.redis
        cursor = "0-0"
        reclaimed: list[tuple[str, dict]] = []
        while True:
            cursor, entries, _ = await redis.xautoclaim(
                self.queue.stream_key, self.queue.group_name, self.queue.consumer_name,
                min_idle_time=self.min_idle_ms, start_id=cursor, count=50,
            )
            reclaimed.extend((entry_id, decode_fields(fields)) for entry_id, fields in entries)
            if cursor == "0-0" or not entries:
                break
        return reclaimed

    async def recover(self, dead_letter: DeadLetterQueue) -> RecoveryResult:
        """Reclaims stale pending entries and splits them into what's still
        worth retrying vs. what's exhausted its budget. Dead-lettered
        entries are ACKed here (removing them from the PEL) — retryable
        ones are NOT acked; the caller acks them after reprocessing."""
        reclaimed = await self._reclaim_stale()
        retryable, dead_lettered = [], []

        for entry_id, payload in reclaimed:
            if await self._delivery_count(entry_id) > self.max_retries:
                await dead_letter.add(entry_id, payload, reason="max_retries_exceeded")
                await self.queue.ack(entry_id)
                dead_lettered.append(entry_id)
            else:
                retryable.append((entry_id, payload))

        return RecoveryResult(retryable=retryable, dead_lettered=dead_lettered)
