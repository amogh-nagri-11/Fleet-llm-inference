from collections import defaultdict
from typing import Optional

from context.models import ContextItem, ContextType


class ContextStore:
    """In-memory context storage, scoped by workflow_id.

    Deliberately not backed by PostgreSQL yet — durable storage arrives in
    Phase 6 alongside memory (REDESIGN.md §18). Phase 3 only needs items to
    survive for the lifetime of a workflow within a single gateway process.
    """

    def __init__(self):
        self._items: dict[str, ContextItem] = {}
        self._by_workflow: dict[str, list[str]] = defaultdict(list)

    def add(self, item: ContextItem) -> ContextItem:
        self._items[item.id] = item
        if item.workflow_id:
            self._by_workflow[item.workflow_id].append(item.id)
        return item

    def get(self, item_id: str) -> Optional[ContextItem]:
        item = self._items.get(item_id)
        if item:
            item.touch()
        return item

    def list_for_workflow(
        self, workflow_id: str, type: Optional[ContextType] = None
    ) -> list[ContextItem]:
        ids = self._by_workflow.get(workflow_id, [])
        items = [self._items[i] for i in ids if i in self._items]
        if type is not None:
            items = [i for i in items if i.type == type]
        return items

    def delete(self, item_id: str) -> bool:
        item = self._items.pop(item_id, None)
        if item is None:
            return False
        if item.workflow_id:
            ids = self._by_workflow.get(item.workflow_id)
            if ids and item_id in ids:
                ids.remove(item_id)
        return True

    def clear_workflow(self, workflow_id: str) -> int:
        ids = self._by_workflow.pop(workflow_id, [])
        for item_id in ids:
            self._items.pop(item_id, None)
        return len(ids)

    def total_tokens(self, workflow_id: str) -> int:
        return sum(i.token_count for i in self.list_for_workflow(workflow_id))
