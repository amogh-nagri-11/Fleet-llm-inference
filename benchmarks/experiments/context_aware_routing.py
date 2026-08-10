#!/usr/bin/env python3
"""
Experiment 5 — Context-Aware Routing (REDESIGN.md §57).

Hypothesis: given workers with different context capacities, Fleet's
context-aware routing (Phase 9) correctly avoids workers that can't hold
a request's context, choosing among only the eligible ones — rather than
picking blindly (or crashing) when a request needs more context than
some workers can provide.

This runs against a real LoadBalancer instance with real (not mocked)
pick_worker() calls — only the *workers* are synthetic, since this
environment only has one real Ollama instance available to test against
(§57's scenario needs multiple workers with different capacities, which
this dev environment can't provide for real). Every routing decision
below is the actual production code path (router/load_balancer.py),
not a simulation of it — see docs/experiments.md for the full scoping
note on why the workers themselves are synthetic.

Usage
-----
    python benchmarks/experiments/context_aware_routing.py
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from router.load_balancer import LoadBalancer, NoCapacityError


@dataclass
class Scenario:
    name: str
    context_tokens: int
    expected_eligible: list[str]


def build_heterogeneous_fleet() -> LoadBalancer:
    """Three workers with different context capacities, matching
    REDESIGN.md §41's exact example: Worker A (32k, low load), Worker B
    (8k), Worker C (32k, heavily loaded)."""
    lb = LoadBalancer(["http://worker-a", "http://worker-b", "http://worker-c"])
    a, b, c = lb.workers
    a.max_context_tokens = 32768
    b.max_context_tokens = 8192
    c.max_context_tokens = 32768
    # "Heavily loaded" isn't a capacity fact — it must NOT affect
    # eligibility, only tie-breaking among eligible workers (§41: a busy
    # worker with capacity stays eligible).
    c.stats.active_requests = 50
    return lb


SCENARIOS = [
    Scenario("small request (100 tokens)", 100, ["http://worker-a", "http://worker-b", "http://worker-c"]),
    Scenario("medium request (16000 tokens)", 16000, ["http://worker-a", "http://worker-c"]),
    Scenario("oversized request (100000 tokens)", 100000, []),
]


def run() -> list[dict]:
    results = []
    for scenario in SCENARIOS:
        lb = build_heterogeneous_fleet()
        eligible = [w.stats.url for w in lb._available_workers(context_tokens=scenario.context_tokens)]

        try:
            picked = lb.pick_worker(context_tokens=scenario.context_tokens)
            picked_url = picked.stats.url
            rejected = False
        except NoCapacityError:
            picked_url = None
            rejected = True

        results.append({
            "scenario": scenario.name,
            "context_tokens": scenario.context_tokens,
            "eligible": eligible,
            "expected_eligible": scenario.expected_eligible,
            "matches_expectation": eligible == scenario.expected_eligible,
            "picked": picked_url,
            "rejected": rejected,
        })
    return results


def report(results: list[dict]) -> None:
    print("Experiment 5 — Context-Aware Routing")
    print("=" * 70)
    for r in results:
        print(f"\nScenario: {r['scenario']}")
        print(f"  Eligible workers:  {r['eligible'] or '(none)'}")
        print(f"  Matches §41 expectation: {r['matches_expectation']}")
        if r["rejected"]:
            print("  Result: correctly rejected (NoCapacityError) — no worker has enough capacity")
        else:
            print(f"  Result: picked {r['picked']}")
    print()
    all_match = all(r["matches_expectation"] for r in results)
    print(f"All scenarios matched §41's expected eligibility: {all_match}")


def main():
    report(run())


if __name__ == "__main__":
    main()
