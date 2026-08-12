from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from config import settings
from context.models import ContextItem, ContextType
from router.load_balancer import load_balancer

# Injected rather than hardcoded to a live model call, so compress_items()
# stays testable without a running inference backend. llm_summarizer()
# below is the real implementation passed in production.
Summarizer = Callable[[str], Awaitable[str]]


@dataclass
class CompressionResult:
    original_items: list[ContextItem]
    summary_item: ContextItem
    tokens_before: int = field(init=False)
    tokens_after: int = field(init=False)

    def __post_init__(self):
        self.tokens_before = sum(i.token_count for i in self.original_items)
        self.tokens_after = self.summary_item.token_count

    @property
    def tokens_saved(self) -> int:
        return self.tokens_before - self.tokens_after


async def compress_items(
    items: list[ContextItem],
    summarizer: Summarizer,
    workflow_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> CompressionResult:
    """REDESIGN.md no. 13: old conversation -> summary -> compressed context.
    Must be measurable (§13's own instruction) — CompressionResult tracks
    exactly how many tokens were saved, not a guess."""
    if not items:
        raise ValueError("compress_items requires at least one item")

    combined = "\n\n".join(i.content for i in items)
    summary_text = await summarizer(combined)

    summary_item = ContextItem(
        type=ContextType.SUMMARY,
        content=summary_text,
        source="compression",
        workflow_id=workflow_id if workflow_id is not None else items[0].workflow_id,
        agent_id=agent_id if agent_id is not None else items[0].agent_id,
        importance=max(i.importance for i in items),
    )
    return CompressionResult(original_items=items, summary_item=summary_item)


async def llm_summarizer(text: str, model: Optional[str] = None) -> str:
    """The real summarization backend (REDESIGN.md §13: 'the summarization
    backend can use the same inference infrastructure') — this is the
    recursive workflow the doc describes: Fleet requests summarization,
    which is itself an inference request through the existing worker
    layer, not a separate summarization system.

    pick_worker() is deliberately outside the try/except below — same
    fixed pattern as gateway/routes.py and workers/worker_pool.py (Phase
    1/2): a "no workers available" error shouldn't be blamed on a worker
    that was never actually picked.
    """
    worker = load_balancer.pick_worker()
    prompt = (
        "Summarize the following conversation/context concisely, keeping "
        "only information likely to matter for continuing the task:\n\n" + text
    )
    try:
        result = await worker.generate(model=model or settings.DEFAULT_MODEL, prompt=prompt)
        load_balancer.record_success(worker.stats.url)
        return result["response"]
    except RuntimeError:
        load_balancer.record_failure(worker.stats.url)
        raise
