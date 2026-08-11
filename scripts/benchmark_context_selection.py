#!/usr/bin/env python3
"""
Deterministic, offline benchmark for the Phase 4 context selection policies
(context/selection.py) — REDESIGN.md §72 Phase 4 "benchmark it".

This does NOT call a live model. It builds a synthetic context pool shaped
like a long-running coding-agent workflow (REDESIGN.md §55's flavor — steady
accumulation of conversation, file reads, and tool output, with a handful of
high-importance items like the original task and recent errors mixed in) and
compares how each policy packs it into a fixed token budget.

This validates the packing algorithm's *mechanics* (does it respect the
budget, does it prefer higher-value items, how many tokens does it save)
with real, reproducible numbers. It is NOT the REDESIGN.md §53 "Full History
vs Budgeted Context" experiment, which measures actual task success against
a live model — that needs real agent workloads and belongs in Phase 14.

Usage
-----
    python scripts/benchmark_context_selection.py
    python scripts/benchmark_context_selection.py --steps 50 --budget 4000
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context.models import ContextItem, ContextType
from context.selection import POLICIES, select_context


def build_synthetic_workflow(steps: int, seed: int) -> list[ContextItem]:
    """Simulate a coding-agent workflow accumulating context over `steps`
    turns: mostly routine conversation/tool output, occasionally a large
    tool dump, with the original task instruction kept high-importance
    throughout (mirrors REDESIGN.md §32 "tool output can become enormous")."""
    rng = random.Random(seed)
    items = []

    task = ContextItem(
        type=ContextType.INSTRUCTION,
        content="Fix the failing authentication tests." * 5,
        importance=1.0,
        relevance=0.9,
        source="user_task",
    )
    task.created_at = 0.0
    items.append(task)

    for step in range(steps):
        kind = rng.choices(
            [ContextType.CONVERSATION, ContextType.TOOL_RESULT, ContextType.ERROR],
            weights=[0.6, 0.3, 0.1],
        )[0]
        size = rng.choice([200, 400, 800]) if kind != ContextType.TOOL_RESULT else rng.choice(
            [800, 2000, 8000]
        )
        importance = 0.85 if kind == ContextType.ERROR else rng.uniform(0.2, 0.6)
        relevance = rng.uniform(0.3, 0.9)

        item = ContextItem(
            type=kind,
            content="x" * size,
            importance=importance,
            relevance=relevance,
            source=f"step_{step}",
        )
        item.created_at = float(step + 1)
        items.append(item)

    return items


def run(steps: int, budget: int, seed: int) -> None:
    items = build_synthetic_workflow(steps, seed)
    candidate_tokens = sum(i.token_count for i in items)

    print(f"Synthetic workflow: {len(items)} items, {candidate_tokens} candidate tokens, "
          f"budget={budget}\n")

    header = f"{'Policy':<14}{'Selected Tok':>14}{'Saved Tok':>12}{'Items':>10}{'Avg Importance':>16}"
    print(header)
    print("-" * len(header))

    for policy in POLICIES:
        result = select_context(items, budget_tokens=budget, policy=policy)
        avg_importance = (
            sum(i.importance for i in result.selected) / len(result.selected)
            if result.selected else 0.0
        )
        print(
            f"{policy:<14}{result.selected_tokens:>14}{result.tokens_saved:>12}"
            f"{f'{len(result.selected)}/{len(items)}':>10}{avg_importance:>16.3f}"
        )


def main():
    p = argparse.ArgumentParser(description="Benchmark context selection policies (offline, synthetic)")
    p.add_argument("--steps", type=int, default=30, help="simulated workflow turns")
    p.add_argument("--budget", type=int, default=4000, help="context budget in tokens")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    run(args.steps, args.budget, args.seed)


if __name__ == "__main__":
    main()
