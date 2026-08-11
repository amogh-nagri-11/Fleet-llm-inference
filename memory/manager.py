import time
from typing import Optional

from context.models import ContextItem
from memory.models import MemoryItem, MemoryKind
from memory.retrieval import retrieve_relevant_memories, to_context_items
from memory.store import MemoryStore


class MemoryManager:
    """Coordinates memory capture and retrieval for a workflow.

    Through Phase 7: recording working/episodic memory into durable
    storage, listing it back, and — new this phase — ranking + budgeting +
    injecting it into the context pipeline (REDESIGN.md §19). Memories are
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

    async def get_relevant_memories(
        self,
        workflow_id: str,
        budget_tokens: int,
        query: str = "",
        kind: Optional[MemoryKind] = None,
        weights: Optional[dict] = None,
    ) -> list[MemoryItem]:
        """Full §19 pipeline: retrieve candidates for the workflow, rank
        them (relevance/importance/recency/access_frequency, §21), and
        select down to `budget_tokens`. Not wired into the live request
        path yet — that's Phase 9, same as context/."""
        candidates = await self.list_for_workflow(workflow_id, kind=kind)
        return retrieve_relevant_memories(candidates, budget_tokens, query=query, weights=weights)

    async def get_relevant_context(
        self,
        workflow_id: str,
        budget_tokens: int,
        query: str = "",
        kind: Optional[MemoryKind] = None,
        weights: Optional[dict] = None,
    ) -> list[ContextItem]:
        """get_relevant_memories() + the §19 'inject' step — ready-to-use
        ContextItems (type=MEMORY)."""
        memories = await self.get_relevant_memories(
            workflow_id, budget_tokens, query=query, kind=kind, weights=weights
        )
        return to_context_items(memories)


memory_manager = MemoryManager()
