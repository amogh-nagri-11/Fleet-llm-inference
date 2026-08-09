from typing import Optional

from context.models import ContextItem, ContextType
from context.selection import SelectionResult, select_context
from context.store import ContextStore


class ContextManager:
    """Coordinates context capture and retrieval for a workflow.

    Through Phase 4: turning inference-relevant content into stored
    ContextItems, returning the unranked candidate pool, and — new this
    phase — selecting a budget-fitting subset via context/selection.py.
    Compression (Phase 8) and memory retrieval (Phase 6/7) still aren't
    part of this pipeline yet — REDESIGN.md §8 lists them as Context Engine
    responsibilities, but the doc's own phase breakdown (§72) builds them
    incrementally rather than all at once.
    """

    def __init__(self, store: Optional[ContextStore] = None):
        self.store = store or ContextStore()

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


context_manager = ContextManager()
