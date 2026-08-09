import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from context.models import ContextItem, ContextType, estimate_tokens


class ArtifactType(str, Enum):
    """REDESIGN.md §34 — content large enough to live outside the prompt."""
    FILE = "file"
    TOOL_OUTPUT = "tool_output"
    LOG = "log"
    DOCUMENT = "document"


DEFAULT_SUMMARY_KEYWORDS = ("error", "fail", "exception", "traceback")


def summarize_text(
    content: str, keywords=DEFAULT_SUMMARY_KEYWORDS, max_lines: int = 10
) -> str:
    """Deterministic, rule-based summary (REDESIGN.md §32/§33) — no LLM
    call. Real semantic summarization is Phase 8's job, which explicitly
    routes through the inference layer; Phase 5 only needs *something*
    small and useful to put in context instead of the raw dump. Mirrors
    §33's worked example directly: pull out the lines that look like
    failures rather than showing everything."""
    lines = content.splitlines() or [content]
    total = len(lines)
    matched = [l for l in lines if any(k in l.lower() for k in keywords)]

    if matched:
        shown = matched[:max_lines]
        header = f"{total} lines total, {len(matched)} matched {list(keywords)}:"
        hidden = len(matched) - len(shown)
    else:
        shown = lines[:max_lines]
        header = f"{total} lines total, first {len(shown)} shown:"
        hidden = total - len(shown)

    parts = [header, *shown]
    if hidden > 0:
        parts.append(f"... ({hidden} more not shown)")
    return "\n".join(parts)


@dataclass
class Artifact:
    """Large content stored outside the prompt (REDESIGN.md §34). Only a
    small reference to this — not the content itself — goes into context;
    the full artifact stays retrievable by id (§33)."""
    type: ArtifactType
    content: str
    source: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    summary: str = field(init=False, default="")
    token_count: int = field(init=False, default=0)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.summary = summarize_text(self.content)
        self.token_count = estimate_tokens(self.content)


class ArtifactStore:
    """In-memory artifact storage. Same non-durability caveat as
    ContextStore (Phase 3) — PostgreSQL arrives in Phase 6."""

    def __init__(self):
        self._artifacts: dict[str, Artifact] = {}

    def create(self, type: ArtifactType, content: str, source: Optional[str] = None) -> Artifact:
        artifact = Artifact(type=type, content=content, source=source)
        self._artifacts[artifact.id] = artifact
        return artifact

    def get(self, artifact_id: str) -> Optional[Artifact]:
        return self._artifacts.get(artifact_id)

    def get_excerpt(self, artifact_id: str, start: int, end: int) -> Optional[str]:
        """Line-range retrieval (REDESIGN.md §35 `relevant_ranges`) — lets a
        caller pull a specific slice of a large artifact on demand instead
        of the whole thing."""
        artifact = self.get(artifact_id)
        if artifact is None:
            return None
        lines = artifact.content.splitlines()
        return "\n".join(lines[start:end])

    def to_reference_item(
        self, artifact: Artifact, workflow_id: Optional[str], agent_id: Optional[str] = None
    ) -> ContextItem:
        """The small item that actually goes into context — summary +
        artifact_id, not the raw content (§33/§35)."""
        return ContextItem(
            type=ContextType.SUMMARY,
            content=artifact.summary,
            source=artifact.source,
            workflow_id=workflow_id,
            agent_id=agent_id,
            artifact_id=artifact.id,
        )
