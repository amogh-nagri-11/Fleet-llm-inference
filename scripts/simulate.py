#!/usr/bin/env python3
"""
Agent workload simulator (REDESIGN.md §7-8/§72 Phase 12).

Simulates N concurrent agents generating traffic against a real running
Fleet gateway — every request goes through client/fleet_client.py, the
same public HTTP API a real caller uses. This is deliberately not a
separate benchmark path that bypasses Fleet (§8: "do not create a fake
benchmark path that bypasses Fleet").

Workload profiles (§7):
  coding    — bursts of several small requests per agent, short gaps
              (an agent interleaving LLM calls with tool calls).
  research  — fewer, larger requests, longer gaps (an agent pausing
              between calls to "search").
  batch     — many small, low-effort requests, minimal think time.
  mixed     — each simulated agent randomly gets one of the above.

Each simulated agent keeps its own agent_id/workflow_id and loops through
its workload pattern for the full --duration, not just once.

This is NOT REDESIGN.md §37's Experiment/benchmark harness (Phase 14) —
it doesn't compare scheduling policies or measure task success. It's the
traffic generator those experiments would sit on top of.

Examples
--------
    python scripts/simulate.py --agents 5 --workload mixed --duration 30
    python scripts/simulate.py --agents 20 --workload coding --duration 60 --arrival-rate 5
"""
import argparse
import asyncio
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.fleet_client import FleetClient

WORKLOADS = ("coding", "research", "batch", "mixed")


@dataclass
class WorkloadProfile:
    name: str
    prompt_lengths: tuple[int, int]   # (min, max) characters
    think_time: tuple[float, float]   # (min, max) seconds between requests
    requests_per_burst: tuple[int, int]


PROFILES = {
    "coding": WorkloadProfile("coding", prompt_lengths=(60, 200), think_time=(0.2, 0.8), requests_per_burst=(3, 6)),
    "research": WorkloadProfile("research", prompt_lengths=(300, 700), think_time=(1.5, 3.0), requests_per_burst=(2, 3)),
    "batch": WorkloadProfile("batch", prompt_lengths=(20, 60), think_time=(0.0, 0.2), requests_per_burst=(5, 10)),
}


def make_prompt(profile: WorkloadProfile) -> str:
    length = random.randint(*profile.prompt_lengths)
    base = "Summarize the following in one sentence: " + ("context data. " * 60)
    return base[:length]


def assign_workloads(num_agents: int, workload: str) -> list[str]:
    if workload != "mixed":
        return [workload] * num_agents
    return [random.choice(list(PROFILES)) for _ in range(num_agents)]


@dataclass
class RequestResult:
    workload: str
    latency: float
    ok: bool
    status: int = 200


@dataclass
class SimResults:
    results: list[RequestResult] = field(default_factory=list)

    def add(self, r: RequestResult) -> None:
        self.results.append(r)


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


async def run_agent(
    agent_index: int,
    workload_name: str,
    client: FleetClient,
    model: str,
    end_time: float,
    results: SimResults,
    start_delay: float,
) -> None:
    await asyncio.sleep(start_delay)

    profile = PROFILES[workload_name]
    agent_id = f"{workload_name}-agent-{agent_index}"
    workflow_id = f"wf-{agent_id}-{uuid.uuid4().hex[:6]}"

    while time.time() < end_time:
        burst_size = random.randint(*profile.requests_per_burst)
        for _ in range(burst_size):
            if time.time() >= end_time:
                break
            prompt = make_prompt(profile)
            start = time.perf_counter()
            try:
                await client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model=model, agent_id=agent_id, workflow_id=workflow_id,
                )
                latency = time.perf_counter() - start
                results.add(RequestResult(workload_name, latency, ok=True))
            except Exception:
                latency = time.perf_counter() - start
                results.add(RequestResult(workload_name, latency, ok=False, status=0))

            await asyncio.sleep(random.uniform(*profile.think_time))


async def run(args) -> None:
    results = SimResults()
    end_time = time.time() + args.duration

    agent_workloads = assign_workloads(args.agents, args.workload)
    counts = Counter(agent_workloads)
    print("Fleet Simulation")
    print("-" * 16)
    print(f"Agents:      {args.agents}  ({dict(counts)})")
    print(f"Duration:    {args.duration}s")
    print(f"Base URL:    {args.base_url}")
    print("Running...\n")

    async with FleetClient(base_url=args.base_url, api_key=args.api_key, timeout=args.timeout) as client:
        tasks = []
        for i, w in enumerate(agent_workloads):
            start_delay = (i / args.arrival_rate) if args.arrival_rate else 0.0
            tasks.append(run_agent(i, w, client, args.model, end_time, results, start_delay))

        wall_start = time.perf_counter()
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - wall_start

    report(results, wall, args)


def report(results: SimResults, wall: float, args) -> None:
    all_latencies = sorted(r.latency for r in results.results if r.ok)
    total = len(results.results)
    failed = sum(1 for r in results.results if not r.ok)

    print("-" * 48)
    print(f"Requests:        {total}")
    print(f"Failed:          {failed}")
    print(f"Throughput:      {total / wall:.2f} req/s")
    if all_latencies:
        print(f"P50 latency:     {percentile(all_latencies, 50) * 1000:8.1f} ms")
        print(f"P95 latency:     {percentile(all_latencies, 95) * 1000:8.1f} ms")
        print(f"P99 latency:     {percentile(all_latencies, 99) * 1000:8.1f} ms")
    print("-" * 48)

    print("\nPer-workload breakdown:")
    for name in PROFILES:
        subset = sorted(r.latency for r in results.results if r.workload == name and r.ok)
        sub_failed = sum(1 for r in results.results if r.workload == name and not r.ok)
        if not subset and not sub_failed:
            continue
        p50 = percentile(subset, 50) * 1000 if subset else 0.0
        print(f"  {name:<10} requests={len(subset) + sub_failed:<5} failed={sub_failed:<4} p50={p50:7.1f}ms")

    print(
        "\nNote: latency here is full request round-trip, not TTFT (no streaming "
        "yet), and there's no SLO-violation count (§39 priorities/SLOs aren't "
        "implemented) — reporting only what's actually measurable, not REDESIGN.md "
        "§42's example fields verbatim."
    )


def main():
    p = argparse.ArgumentParser(description="Fleet agent workload simulator")
    p.add_argument("--agents", type=int, default=5)
    p.add_argument("--workload", choices=WORKLOADS, default="mixed")
    p.add_argument("--duration", type=float, default=30.0, help="seconds")
    p.add_argument("--arrival-rate", type=float, default=0.0,
                    help="agents started per second (0 = all at once)")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--api-key", default="dev-key")
    p.add_argument("--model", default="llama3:latest")
    p.add_argument("--timeout", type=float, default=180.0)
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
