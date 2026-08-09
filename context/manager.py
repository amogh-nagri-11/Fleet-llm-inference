from typing import Optional

from context.models import ContextItem, ContextType
from context.store import ContextStore


class ContextManager:
    """Coordinates context capture and retrieval for a workflow.

    Phase 3 scope only: turning inference-relevant content into stored
    ContextItems, and returning the unranked candidate pool for a workflow.
    Ranking, budgeting (Phase 4), and compression (Phase 8) are deliberately
    not implemented here yet — REDESIGN.md §8 lists them as Context Engine
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
        """Unranked, unbudgeted pool of everything stored for a workflow.
        Phase 4 adds selection/budgeting on top of this."""
        return self.store.list_for_workflow(workflow_id)

    def total_tokens(self, workflow_id: str) -> int:
        return self.store.total_tokens(workflow_id)


context_manager = ContextManager()
