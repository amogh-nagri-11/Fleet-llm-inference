import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MemoryKind(str, Enum):
    """REDESIGN.md §14 — v2 scope is working + episodic only (§0.2);
    semantic memory (embeddings/vector search) is deferred."""
    WORKING = "working"
    EPISODIC = "episodic"


@dataclass
class MemoryItem:
    """Durable memory record (REDESIGN.md §15/§16/§20). Written explicitly
    by the calling agent/harness — Fleet does not auto-extract memories
    from arbitrary text (§14). No `relevance` field: unlike ContextItem,
    relevance is judged against a specific request at retrieval time
    (Phase 7), not an intrinsic property to store."""
    kind: MemoryKind
    content: str
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    importance: float = 0.5
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    access_count: int = 0
    # Working memory only (§15 — "short-lived... expire when appropriate").
    # None for episodic memory, which persists indefinitely.
    expires_at: Optional[float] = None
