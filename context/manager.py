from typing import Optional

from context.artifacts import Artifact, ArtifactStore, ArtifactType
from context.compression import CompressionResult, Summarizer, compress_items
from context.models import ContextItem, ContextType
from context.selection import SelectionResult, select_context
from context.store import ContextStore


class ContextManager:
    """Coordinates context capture and retrieval for a workflow.

    Through Phase 8: turning inference-relevant content into stored
    ContextItems, returning the unranked candidate pool, selecting a
    budget-fitting subset (Phase 4), externalizing large content as
    artifacts (Phase 5), and — new this phase — compressing old context
    into a single summary item via the inference layer (Phase 8). Memory
    retrieval (Phase 7) lives in MemoryManager and isn't merged into this
    pipeline yet — that integration is Phase 9.
    """

    def __init__(
        self, store: Optional[ContextStore] = None, artifact_store: Optional[ArtifactStore] = None
    ):
        self.store = store or ContextStore()
        self.artifact_store = artifact_store or ArtifactStore()

    def record(
        self,
        content: str,
        type: ContextType,
        workflow_id: Optional[str],
        agent_id: Optional[str] = None,
        source: Optional[str] = None,
        importance: float = 0.5,
    ) -> ContextItem:
        item = ContextItem(
            type=type,
            content=content,
            workflow_id=workflow_id,
            agent_id=agent_id,
            source=source,
            importance=importance,
        )
        return self.store.add(item)

    def record_artifact(
        self,
        content: str,
        artifact_type: ArtifactType,
        workflow_id: Optional[str],
        agent_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> ContextItem:
        """Store large content (a file, tool output, log, document) as an
        artifact and record only a small reference — summary + artifact_id
        — as context (REDESIGN.md §32-35). Use this instead of record()
        when dumping `content` straight into context would be wasteful;
        the full artifact stays retrievable via get_artifact()."""
        artifact = self.artifact_store.create(artifact_type, content, source=source)
        item = self.artifact_store.to_reference_item(artifact, workflow_id, agent_id)
        return self.store.add(item)

    def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        return self.artifact_store.get(artifact_id)

    def get_artifact_excerpt(self, artifact_id: str, start: int, end: int) -> Optional[str]:
        return self.artifact_store.get_excerpt(artifact_id, start, end)

    def get_candidate_context(self, workflow_id: str) -> list[ContextItem]:
        """Unranked, unbudgeted pool of everything stored for a workflow."""
        return self.store.list_for_workflow(workflow_id)

    def get_budgeted_context(
        self, workflow_id: str, budget_tokens: int, policy: str = "hybrid"
    ) -> SelectionResult:
        """Candidate pool for a workflow, selected down to fit
        `budget_tokens` per REDESIGN.md §9-§12. Not wired into the live
        request path yet — that's Phase 9."""
        candidates = self.get_candidate_context(workflow_id)
        return select_context(candidates, budget_tokens, policy=policy)

    def total_tokens(self, workflow_id: str) -> int:
        return self.store.total_tokens(workflow_id)

    async def compress_old_context(
        self,
        workflow_id: str,
        summarizer: Summarizer,
        max_items: Optional[int] = None,
        context_type: Optional[ContextType] = None,
    ) -> Optional[CompressionResult]:
        """REDESIGN.md §13: old conversation -> summary -> compressed
        context. Takes the oldest items for a workflow (optionally filtered
        to one type, e.g. only CONVERSATION), replaces them in the store
        with a single summary ContextItem. Not triggered automatically —
        the caller decides when compression is worth it, same explicit-
        choice pattern as record() vs record_artifact() (Phase 5). Returns
        None if there's nothing to compress."""
        candidates = self.get_candidate_context(workflow_id)
        if context_type is not None:
            candidates = [c for c in candidates if c.type == context_type]
        candidates.sort(key=lambda i: i.created_at)
        if max_items is not None:
            candidates = candidates[:max_items]

        if not candidates:
            return None

        result = await compress_items(candidates, summarizer, workflow_id=workflow_id)

        for item in result.original_items:
            self.store.delete(item.id)
        self.store.add(result.summary_item)

        return result


context_manager = ContextManager()
