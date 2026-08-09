import asyncio
import json
import redis.asyncio as aioredis
from router.load_balancer import load_balancer
from config import settings


class WorkerPool:
    def __init__(self):
        self.redis = None
        self._task = None
        self.queue_key = "llm:request_queue"
        self.result_prefix = "llm:result:"

    async def connect(self):
        self.redis = await aioredis.from_url(
            f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
            decode_responses=True
        )
        print(f"[WorkerPool] Connected to Redis at {settings.REDIS_HOST}:{settings.REDIS_PORT}")

    async def enqueue(self, request_id: str, payload: dict):
        job = json.dumps({"request_id": request_id, **payload})
        await self.redis.rpush(self.queue_key, job)
        print(f"[WorkerPool] Enqueued request {request_id} | queue_depth={await self.queue_depth()}")

    async def get_result(self, request_id: str, timeout: int = 120) -> dict | None:
        result_key = f"{self.result_prefix}{request_id}"
        # Poll for result
        for _ in range(timeout * 2):
            result = await self.redis.get(result_key)
            if result:
                await self.redis.delete(result_key)
                return json.loads(result)
            await asyncio.sleep(0.5)
        return None

    async def queue_depth(self) -> int:
        return await self.redis.llen(self.queue_key)

    async def _process_loop(self):
        print("[WorkerPool] Processing loop started")
        while True:
            try:
                # Block until a job appears (timeout 1s so loop stays responsive)
                job_raw = await self.redis.blpop(self.queue_key, timeout=1)
                if not job_raw:
                    continue

                _, job_str = job_raw
                job = json.loads(job_str)
                request_id = job.pop("request_id")

                # Pick a worker and process
                try:
                    worker = load_balancer.pick_worker()
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

                # Store result for the waiting request
                result_key = f"{self.result_prefix}{request_id}"
                await self.redis.setex(result_key, 300, json.dumps(result))

            except Exception as e:
                print(f"[WorkerPool] Error in processing loop: {e}")
                await asyncio.sleep(1)

    def start(self):
        self._task = asyncio.create_task(self._process_loop())
        print("[WorkerPool] Started")

    def stop(self):
        if self._task:
            self._task.cancel()


worker_pool = WorkerPool()