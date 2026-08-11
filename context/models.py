import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ContextType(str, Enum):
    """REDESIGN.md §7 — possible ContextItem types."""
    CONVERSATION = "conversation"
    FILE = "file"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    MEMORY = "memory"
    SUMMARY = "summary"
    INSTRUCTION = "instruction"
    TASK_STATE = "task_state"


def estimate_tokens(text: str) -> int:
    """Rough ~4-chars-per-token estimate, not a real tokenizer. Good enough
    for Phase 3 storage; Phase 4 owns the actual budgeting math and can swap
    this out without changing ContextItem's shape."""
    return max(1, len(text) // 4)


@dataclass
class ContextItem:
    """First-class context data, per REDESIGN.md §7. Fleet does not
    interpret `content` — it only tracks enough metadata to select, budget,
    and eventually expire it."""
    type: ContextType
    content: str
    source: Optional[str] = None
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    importance: float = 0.5
    relevance: float = 0.5
    # Set when this item is a small reference to a large stored Artifact
    # (context/artifacts.py, §35) rather than the content itself — lets a
    # caller fetch the full thing back by id when actually needed.
    artifact_id: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    token_count: int = field(init=False, default=0)
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.token_count = estimate_tokens(self.content)

    def touch(self) -> None:
        self.last_accessed_at = time.time()
