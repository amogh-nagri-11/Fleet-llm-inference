import asyncio
import json
import redis.asyncio as aioredis
from router.load_balancer import load_balancer
from job_queue.dead_letter import DeadLetterQueue
from job_queue.retry import RetryPolicy
from job_queue.streams import RedisStreamQueue
from config import settings


class WorkerPool:
    def __init__(self):
        self.redis = None
        self.queue: RedisStreamQueue | None = None
        self.dead_letter: DeadLetterQueue | None = None
        self.retry_policy: RetryPolicy | None = None
        self._task = None
        self._recovery_task = None
        # Stream, not a List — a different Redis key than the pre-Phase-10
        # llm:request_queue, since XADD onto a List-typed key would WRONGTYPE.
        self.queue_key = "llm:stream:request_queue"
        self.dead_letter_key = "llm:stream:dead_letter"
        self.result_prefix = "llm:result:"

    async def connect(self):
        self.redis = await aioredis.from_url(
            f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
            decode_responses=True
        )
        self.queue = RedisStreamQueue(
            stream_key=self.queue_key, group_name=settings.QUEUE_CONSUMER_GROUP
        )
        await self.queue.connect(self.redis)
        self.dead_letter = DeadLetterQueue(key=self.dead_letter_key)
        await self.dead_letter.connect(self.redis)
        self.retry_policy = RetryPolicy(
            self.queue,
            max_retries=settings.QUEUE_MAX_RETRIES,
            min_idle_ms=settings.QUEUE_PENDING_MIN_IDLE_MS,
        )
        print(f"[WorkerPool] Connected to Redis at {settings.REDIS_HOST}:{settings.REDIS_PORT} "
              f"(stream={self.queue_key}, group={settings.QUEUE_CONSUMER_GROUP})")

    async def enqueue(self, request_id: str, payload: dict):
        await self.queue.enqueue({"request_id": request_id, **payload})
        print(f"[WorkerPool] Enqueued request {request_id} | queue_depth={await self.queue_depth()}")

    async def get_result(self, request_id: str, timeout: int = 120) -> dict | None:
        result_key = f"{self.result_prefix}{request_id}"
        # Poll for result. REDESIGN.md §44 flags this as wasteful and
        # suggests pub/sub instead — out of Phase 10's explicit scope
        # (ACK/retry/recovery/dead-letter), left as-is deliberately.
        for _ in range(timeout * 2):
            result = await self.redis.get(result_key)
            if result:
                await self.redis.delete(result_key)
                return json.loads(result)
            await asyncio.sleep(0.5)
        return None

    async def queue_depth(self) -> int:
        return await self.queue.depth()

    async def _process_job(self, payload: dict) -> tuple[str, dict]:
        """Runs one job through the scheduler + worker. Shared by the
        normal consume loop and the recovery loop — a reclaimed job goes
        through identical dispatch logic to a fresh one, which is also
        what makes reprocessing safe: writing the same result key twice
        is a no-op the second time (REDESIGN.md §18 idempotency — no extra
        dedup machinery needed, the result store is already keyed by
        request_id)."""
        job = dict(payload)
        request_id = job.pop("request_id")
        # Agent metadata rides along in the queue payload for
        # tracing/scheduling use, but isn't an inference kwarg — strip it
        # before **job is passed to the worker.
        agent_meta = {
            "agent_id": job.pop("agent_id", None),
            "workflow_id": job.pop("workflow_id", None),
            "parent_request_id": job.pop("parent_request_id", None),
        }
        context_tokens = job.pop("context_tokens", None)
        print(f"[WorkerPool] event=dequeued request_id={request_id} "
              f"agent_id={agent_meta['agent_id']} workflow_id={agent_meta['workflow_id']}")

        try:
            worker = load_balancer.pick_worker(context_tokens=context_tokens)
        except RuntimeError as e:
            result = {"error": str(e)}
        else:
            try:
                if "messages" in job:
                    result = await worker.chat(**job)
                else:
                    result = await worker.generate(**job)
                load_balancer.record_success(worker.stats.url)
            except RuntimeError as e:
                load_balancer.record_failure(worker.stats.url)
                result = {"error": str(e)}

        return request_id, result

    async def _store_result(self, request_id: str, result: dict) -> None:
        result_key = f"{self.result_prefix}{request_id}"
        await self.redis.setex(result_key, 300, json.dumps(result))

    async def _process_loop(self):
        print("[WorkerPool] Processing loop started")
        while True:
            try:
                entries = await self.queue.read(count=1, block_ms=1000)
                if not entries:
                    continue

                entry_id, payload = entries[0]
                request_id, result = await self._process_job(payload)
                await self._store_result(request_id, result)
                await self.queue.ack(entry_id)

            except Exception as e:
                print(f"[WorkerPool] Error in processing loop: {e}")
                await asyncio.sleep(1)

    async def _recovery_loop(self):
        """REDESIGN.md §17: a worker that crashes mid-job shouldn't lose
        it. Runs periodically alongside _process_loop, reclaiming jobs
        whose consumer went idle too long and either retrying or
        dead-lettering them."""
        print("[WorkerPool] Recovery loop started")
        while True:
            try:
                await asyncio.sleep(settings.QUEUE_RECOVERY_INTERVAL_SECONDS)
                recovery = await self.retry_policy.recover(self.dead_letter)

                for entry_id, payload in recovery.retryable:
                    request_id, result = await self._process_job(payload)
                    await self._store_result(request_id, result)
                    await self.queue.ack(entry_id)

                if recovery.retryable or recovery.dead_lettered:
                    print(f"[WorkerPool] Recovery: retried={len(recovery.retryable)} "
                          f"dead_lettered={len(recovery.dead_lettered)}")

            except Exception as e:
                print(f"[WorkerPool] Error in recovery loop: {e}")

    def start(self):
        self._task = asyncio.create_task(self._process_loop())
        self._recovery_task = asyncio.create_task(self._recovery_loop())
        print("[WorkerPool] Started")

    def stop(self):
        if self._task:
            self._task.cancel()
        if self._recovery_task:
            self._recovery_task.cancel()


worker_pool = WorkerPool()
