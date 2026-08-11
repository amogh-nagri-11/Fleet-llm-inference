#!/usr/bin/env python3
"""
Experiment 6 — Agent Bursts (REDESIGN.md §58).

Hypothesis: as concurrent agent count increases, P95 latency and
throughput degrade in a way that reflects real queueing behavior against
a fixed worker pool, not silent failure.

Reuses scripts/simulate.py (Phase 12) directly — real HTTP traffic
against the real running gateway, the same simulator, just invoked at a
couple of scales and reported side by side.

REDESIGN.md §58's example scales (10/50/100/500 agents) assume a fleet of
real workers. This dev environment has exactly one Ollama instance,
CPU-only, in WSL — requests queue up serially behind it (confirmed in
Phase 12's own live run). Scales here are deliberately small (2 and 4
agents) to keep a real run finishing in a reasonable time; the numbers
are genuinely measured at that scale, not a stand-in for what 500 agents
against a real multi-worker fleet would produce. See docs/experiments.md
for the honest caveat on what this experiment can and can't demonstrate
in this environment.

Usage
-----
    python benchmarks/experiments/agent_bursts.py
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import simulate  # noqa: E402


async def run_scale(agents: int, duration: float, base_url: str, model: str) -> dict:
    results = simulate.SimResults()
    end_time = time.time() + duration

    async with simulate.FleetClient(base_url=base_url, api_key="dev-key", timeout=180.0) as client:
        agent_workloads = simulate.assign_workloads(agents, "mixed")
        tasks = [
            simulate.run_agent(i, w, client, model, end_time, results, start_delay=0)
            for i, w in enumerate(agent_workloads)
        ]
        wall_start = time.perf_counter()
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - wall_start

    latencies = sorted(r.latency for r in results.results if r.ok)
    failed = sum(1 for r in results.results if not r.ok)
    return {
        "agents": agents,
        "duration": duration,
        "requests": len(results.results),
        "failed": failed,
        "throughput": len(results.results) / wall if wall > 0 else 0.0,
        "p50_ms": simulate.percentile(latencies, 50) * 1000 if latencies else 0.0,
        "p95_ms": simulate.percentile(latencies, 95) * 1000 if latencies else 0.0,
        "p99_ms": simulate.percentile(latencies, 99) * 1000 if latencies else 0.0,
    }


async def run(base_url: str, model: str) -> list[dict]:
    return [
        await run_scale(2, 20.0, base_url, model),
        await run_scale(4, 20.0, base_url, model),
    ]


def report(results: list[dict]) -> None:
    print("Experiment 6 — Agent Bursts")
    print("=" * 70)
    header = f"{'Agents':<8}{'Requests':>10}{'Failed':>8}{'Throughput':>13}{'P50':>10}{'P95':>10}{'P99':>10}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['agents']:<8}{r['requests']:>10}{r['failed']:>8}"
            f"{r['throughput']:>10.3f}/s{r['p50_ms']:>9.0f}ms{r['p95_ms']:>9.0f}ms{r['p99_ms']:>9.0f}ms"
        )
    print(
        "\nNote: single-worker, CPU-only, WSL dev environment — requests queue "
        "up serially behind the one Ollama instance. These are real measured "
        "numbers at this small scale, not a stand-in for REDESIGN.md §58's "
        "10-500 agent examples against a real multi-worker fleet."
    )


def main():
    p = argparse.ArgumentParser(description="Experiment 6: agent bursts at increasing concurrency")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--model", default="llama3:latest")
    args = p.parse_args()
    results = asyncio.run(run(args.base_url, args.model))
    report(results)


if __name__ == "__main__":
    main()
