import time
from typing import Optional

from memory.models import MemoryItem, MemoryKind
from memory.store import MemoryStore


class MemoryManager:
    """Coordinates memory capture and retrieval for a workflow.

    Phase 6 scope only: recording working/episodic memory into durable
    storage and listing it back. Retrieval/ranking/budget injection into
    the context pipeline is Phase 7 — REDESIGN.md §19 explicitly separates
    "start with simple storage" (this phase) from retrieval. Memories are
    written explicitly by the caller; Fleet does not auto-extract them from
    arbitrary text (§14).
    """

    def __init__(self, store: Optional[MemoryStore] = None):
        self.store = store or MemoryStore()

    async def record_working(
        self,
        content: str,
        workflow_id: Optional[str],
        agent_id: Optional[str] = None,
        importance: float = 0.5,
        ttl_seconds: Optional[float] = None,
    ) -> MemoryItem:
        expires_at = time.time() + ttl_seconds if ttl_seconds is not None else None
        item = MemoryItem(
            kind=MemoryKind.WORKING,
            content=content,
            agent_id=agent_id,
            workflow_id=workflow_id,
            importance=importance,
            expires_at=expires_at,
        )
        return await self.store.add(item)

    async def record_episodic(
        self,
        content: str,
        workflow_id: Optional[str],
        agent_id: Optional[str] = None,
        importance: float = 0.5,
    ) -> MemoryItem:
        item = MemoryItem(
            kind=MemoryKind.EPISODIC,
            content=content,
            agent_id=agent_id,
            workflow_id=workflow_id,
            importance=importance,
        )
        return await self.store.add(item)

    async def list_for_workflow(
        self, workflow_id: str, kind: Optional[MemoryKind] = None
    ) -> list[MemoryItem]:
        return await self.store.list_for_workflow(workflow_id, kind=kind)

    async def purge_expired(self) -> int:
        return await self.store.purge_expired()


memory_manager = MemoryManager()
