#!/usr/bin/env python3
"""
Benchmark experiment runner (REDESIGN.md §71/§72 Phase 14).

Runs one or all of the experiments in benchmarks/experiments/ and prints
their reports. Each experiment is independently runnable too
(`python benchmarks/experiments/<name>.py`) — this is just a single
entrypoint that runs the set REDESIGN.md's Phase 14 asks for.

Usage
-----
    python benchmarks/runner.py                  # all experiments
    python benchmarks/runner.py --only context_budgeting
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))

import context_aware_routing
import context_budgeting
import agent_bursts

EXPERIMENTS = {
    "context_budgeting": context_budgeting,
    "context_aware_routing": context_aware_routing,
    "agent_bursts": agent_bursts,
}


async def run_one(name: str, module, base_url: str, model: str) -> None:
    print(f"\n{'#' * 70}\n# {name}\n{'#' * 70}\n")
    if name == "context_budgeting":
        results = await module.run("http://localhost:11434", model, 400)
    elif name == "context_aware_routing":
        results = module.run()  # sync, no network
    elif name == "agent_bursts":
        results = await module.run(base_url, model)
    else:
        raise ValueError(name)
    module.report(results)


async def run_all(names: list[str], base_url: str, model: str) -> None:
    for name in names:
        await run_one(name, EXPERIMENTS[name], base_url, model)


def main():
    p = argparse.ArgumentParser(description="Run Fleet benchmark experiments")
    p.add_argument("--only", choices=list(EXPERIMENTS), action="append",
                    help="run only this experiment (repeatable); default: all")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--model", default="llama3:latest")
    args = p.parse_args()
    names = args.only or list(EXPERIMENTS)
    asyncio.run(run_all(names, args.base_url, args.model))


if __name__ == "__main__":
    main()
