import time
from typing import Optional

import asyncpg

from memory.models import MemoryItem, MemoryKind


def _row_to_item(row) -> MemoryItem:
    return MemoryItem(
        kind=MemoryKind(row["kind"]),
        content=row["content"],
        agent_id=row["agent_id"],
        workflow_id=row["workflow_id"],
        importance=row["importance"],
        id=row["id"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        access_count=row["access_count"],
        expires_at=row["expires_at"],
    )


class MemoryStore:
    """PostgreSQL-backed durable storage for working/episodic memory
    (REDESIGN.md §18). No migration framework yet — ensure_schema() runs
    idempotent DDL, matching the project's "start with simple storage"
    instruction for this phase. Semantic memory / vector search stays out
    of scope (§0.2); the `memories` table has no embedding column."""

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self, **kwargs) -> None:
        self.pool = await asyncpg.create_pool(**kwargs)
        await self.ensure_schema()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def ensure_schema(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    agent_id TEXT,
                    workflow_id TEXT,
                    importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    created_at DOUBLE PRECISION NOT NULL,
                    last_used_at DOUBLE PRECISION NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    expires_at DOUBLE PRECISION
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_workflow ON memories (workflow_id)"
            )

    async def add(self, item: MemoryItem) -> MemoryItem:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memories
                    (id, kind, content, agent_id, workflow_id, importance,
                     created_at, last_used_at, access_count, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                item.id, item.kind.value, item.content, item.agent_id, item.workflow_id,
                item.importance, item.created_at, item.last_used_at, item.access_count,
                item.expires_at,
            )
        return item

    async def get(self, item_id: str) -> Optional[MemoryItem]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM memories WHERE id = $1", item_id)
            if row is None:
                return None
            now = time.time()
            await conn.execute(
                "UPDATE memories SET last_used_at = $1, access_count = access_count + 1 "
                "WHERE id = $2",
                now, item_id,
            )
        item = _row_to_item(row)
        item.last_used_at = now
        item.access_count += 1
        return item

    async def list_for_workflow(
        self, workflow_id: str, kind: Optional[MemoryKind] = None
    ) -> list[MemoryItem]:
        async with self.pool.acquire() as conn:
            if kind is not None:
                rows = await conn.fetch(
                    "SELECT * FROM memories WHERE workflow_id = $1 AND kind = $2 "
                    "ORDER BY created_at",
                    workflow_id, kind.value,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM memories WHERE workflow_id = $1 ORDER BY created_at",
                    workflow_id,
                )
        return [_row_to_item(r) for r in rows]

    async def delete(self, item_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM memories WHERE id = $1", item_id)
        return result == "DELETE 1"

    async def purge_expired(self, now: Optional[float] = None) -> int:
        """Delete working-memory rows past their expires_at (§15). Episodic
        memory has no expires_at and is never touched by this."""
        now = now if now is not None else time.time()
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= $1", now
            )
        return int(result.split(" ")[-1])
