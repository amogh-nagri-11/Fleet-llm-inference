#!/usr/bin/env python3
"""
Experiment 1 — Full History vs Budget-Aware Context (REDESIGN.md §53/§60).

Hypothesis: for a task where only a small fraction of accumulated context
is actually relevant, budget-aware selection (context/selection.py,
Phase 4) sends far fewer tokens than dumping the full history, without
losing task success.

This calls context/selection.py directly and talks to Ollama directly
(not through the live gateway) — REDESIGN.md's §53 comparison is a
context-*library* question, and as of Phase 13 nothing in the live
request path actually applies context selection to what gets sent to a
model (confirmed by the Phase-12 brutal audit: gateway/routes.py only
records raw prompts and counts tokens for routing, it doesn't select/
budget them). Framing this as a gateway benchmark would misrepresent
what's actually wired live — see docs/experiments.md for the full
scoping note.

"Task success" is a simple, objective keyword check on the model's own
diagnosis of a real, planted bug (examples/coding_agent/sandbox_repo/
calculator.py's add() function) — not a subjective LLM-judge, matching
REDESIGN.md §61's "objective success criteria" requirement.

Usage
-----
    python benchmarks/experiments/context_budgeting.py
    python benchmarks/experiments/context_budgeting.py --model llama3:latest --budget 400
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from context.models import ContextItem, ContextType
from context.selection import SelectionResult, select_context
from workers.ollama_client import OllamaClient

SANDBOX_CALCULATOR = (
    Path(__file__).resolve().parent.parent.parent
    / "examples" / "coding_agent" / "sandbox_repo" / "calculator.py"
)

# Realistic filler: an agent that has been chatting about unrelated things
# before getting to the actual task, same shape as a long-running session
# accumulating irrelevant history (REDESIGN.md §2's motivating example).
FILLER_TOPICS = [
    "What's a good pasta recipe for tonight?",
    "How do I center a div in CSS?",
    "What's the capital of France?",
    "Can you explain how photosynthesis works?",
    "What's the difference between TCP and UDP?",
    "Recommend a good sci-fi book.",
    "How do I fix a leaky faucet?",
    "What's the best way to learn a new language?",
    "Explain the offside rule in soccer.",
    "What's a good workout routine for beginners?",
    "How does compound interest work?",
    "What's the weather usually like in autumn?",
    "Can you summarize the plot of Hamlet?",
    "How do airplanes stay in the air?",
    "What's a good beginner recipe for bread?",
    "Explain how vaccines work.",
    "What's the tallest mountain in the world?",
    "How do I organize a small bookshelf?",
    "What's a good stretch routine after running?",
    "Explain how a refrigerator keeps things cold.",
]

TASK_PROMPT = (
    "Fix the failing test in calculator.py. In one short sentence, "
    "what exactly is wrong with the add function?"
)

SUCCESS_KEYWORDS = ("subtract", "minus", "- b", "wrong operator", "should add", "incorrectly")


def build_context_pool(workflow_id: str = "experiment-1") -> list[ContextItem]:
    items = []
    for i, topic in enumerate(FILLER_TOPICS):
        filler = ContextItem(
            type=ContextType.CONVERSATION,
            content=f"User: {topic}\nAssistant: [a normal, unrelated response about {topic}]",
            importance=0.2,
            relevance=0.15,
            workflow_id=workflow_id,
        )
        filler.created_at = float(i)
        items.append(filler)

    calculator_content = SANDBOX_CALCULATOR.read_text()
    file_item = ContextItem(
        type=ContextType.FILE,
        content=f"calculator.py:\n{calculator_content}",
        importance=0.9,
        relevance=0.9,
        workflow_id=workflow_id,
        source="calculator.py",
    )
    file_item.created_at = float(len(FILLER_TOPICS))
    items.append(file_item)

    return items


def task_succeeded(response_text: str) -> bool:
    lowered = response_text.lower()
    return any(k in lowered for k in SUCCESS_KEYWORDS)


async def run_strategy(
    policy: str, budget_tokens: int, items: list[ContextItem], worker: OllamaClient, model: str
) -> dict:
    selection: SelectionResult = select_context(items, budget_tokens=budget_tokens, policy=policy)
    context_text = "\n\n".join(i.content for i in selection.selected)
    prompt = f"{context_text}\n\n{TASK_PROMPT}"

    start = time.perf_counter()
    result = await worker.generate(model=model, prompt=prompt)
    latency = time.perf_counter() - start

    return {
        "policy": policy,
        "budget_tokens": budget_tokens,
        "items_selected": len(selection.selected),
        "items_total": len(items),
        "tokens_estimated": selection.selected_tokens,
        "tokens_actual": result["prompt_tokens"],
        "latency_s": round(latency, 2),
        "response": result["response"],
        "task_success": task_succeeded(result["response"]),
    }


async def run(base_url: str, model: str, budget: int) -> list[dict]:
    items = build_context_pool()
    worker = OllamaClient(base_url)

    return [
        await run_strategy("full", budget_tokens=1_000_000, items=items, worker=worker, model=model),
        await run_strategy("hybrid", budget_tokens=budget, items=items, worker=worker, model=model),
    ]


def report(results: list[dict]) -> None:
    print("Experiment 1 — Full History vs Budget-Aware Context")
    print("=" * 70)
    header = (
        f"{'Policy':<10}{'Items':>8}{'Tokens(est)':>13}{'Tokens(actual)':>16}"
        f"{'Latency':>10}{'Success':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        items_str = f"{r['items_selected']}/{r['items_total']}"
        print(
            f"{r['policy']:<10}{items_str:>8}{r['tokens_estimated']:>13}"
            f"{r['tokens_actual']:>16}{r['latency_s']:>9}s{str(r['task_success']):>10}"
        )
    print()
    for r in results:
        print(f"--- {r['policy']} response ---")
        print(r["response"].strip())
        print()


def main():
    p = argparse.ArgumentParser(description="Experiment 1: full history vs budget-aware context")
    p.add_argument("--base-url", default="http://localhost:11434")
    p.add_argument("--model", default="llama3:latest")
    p.add_argument("--budget", type=int, default=400, help="token budget for the hybrid policy")
    args = p.parse_args()
    results = asyncio.run(run(args.base_url, args.model, args.budget))
    report(results)


if __name__ == "__main__":
    main()
