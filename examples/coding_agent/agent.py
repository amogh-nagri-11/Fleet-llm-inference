#!/usr/bin/env python3
"""
Reference coding agent (REDESIGN.md §28-31/§72 Phase 11).

A deliberately simple demonstration of the Fleet loop from §31:

    task -> Fleet context request -> LLM -> tool call -> tool result
         -> Fleet context update -> next LLM call -> ... -> finish

Fleet is the main project here, not this agent (§28) — this script exists
only to prove the loop works against a real, running Fleet gateway and a
real model, using the same public API any external client would use
(client/fleet_client.py, the thin §64 SDK). It is not a general-purpose
agent framework: the four tools are hardcoded (examples/coding_agent/
tools.py), and this script is a fixed sequence of steps, not a planner.

The "understand the task" and "analyze/summarize" steps below are real
inference calls through Fleet. The actual code fix is scripted rather than
parsed from the model's freeform response — reliably extracting an exact
patch from an 8B model's prose is its own hard problem, unrelated to what
this demo is for (Fleet's infrastructure, not code-fixing intelligence).

Usage
-----
    python examples/coding_agent/agent.py
    python examples/coding_agent/agent.py --base-url http://localhost:8000 --model llama3:latest
"""
import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from client.fleet_client import FleetClient
from examples.coding_agent import tools

AGENT_ID = "reference-coding-agent"
TASK = "Fix the failing test in calculator.py"

BUGGY_ADD = "def add(a, b):\n    return a - b"
FIXED_ADD = "def add(a, b):\n    return a + b"


async def run(base_url: str, api_key: str, model: str) -> None:
    workflow_id = f"coding-task-{uuid.uuid4().hex[:8]}"
    print(f"Task: {TASK}")
    print(f"workflow_id: {workflow_id}\n")

    async with FleetClient(base_url=base_url, api_key=api_key) as client:
        # Step 1 (real LLM call via Fleet): understand the task.
        understanding = await client.chat(
            messages=[{
                "role": "user",
                "content": f"A user asked: '{TASK}'. In one short sentence, what needs to be done?",
            }],
            model=model, agent_id=AGENT_ID, workflow_id=workflow_id,
        )
        print(f"[Agent] Understanding: {understanding['message']['content'].strip()}\n")

        # Step 2 (tool): inspect the file mentioned in the task.
        content = tools.read_file("calculator.py")
        print(f"[Tool] read_file(calculator.py):\n{content}")

        # Step 3 (real LLM call via Fleet): analyze the bug.
        analysis = await client.chat(
            messages=[{
                "role": "user",
                "content": (
                    f"Task: {TASK}\n\nHere is calculator.py:\n{content}\n"
                    "In one short sentence, what's wrong with the add function?"
                ),
            }],
            model=model, agent_id=AGENT_ID, workflow_id=workflow_id,
        )
        print(f"[Agent] Analysis: {analysis['message']['content'].strip()}\n")

        # Step 4 (tool): apply the fix — scripted, see module docstring.
        if BUGGY_ADD not in content:
            print("[Tool] write_file skipped — bug already fixed (rerun after a previous run)\n")
        else:
            tools.write_file("calculator.py", content.replace(BUGGY_ADD, FIXED_ADD, 1))
            print("[Tool] write_file(calculator.py) — applied fix\n")

        # Step 5 (tool): run tests for real.
        test_result = tools.run_tests()
        status = "PASSED" if test_result["passed"] else "FAILED"
        print(f"[Tool] run_tests(): {status}\n{test_result['output']}\n")

        # Step 6 (real LLM call via Fleet): summarize the outcome.
        summary = await client.chat(
            messages=[{
                "role": "user",
                "content": f"Tests {status.lower()} after the fix. Summarize what happened in one sentence.",
            }],
            model=model, agent_id=AGENT_ID, workflow_id=workflow_id,
        )
        print(f"[Agent] Summary: {summary['message']['content'].strip()}")


def main():
    p = argparse.ArgumentParser(description="Fleet reference coding agent")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--api-key", default="dev-key")
    p.add_argument("--model", default="llama3:latest")
    args = p.parse_args()
    asyncio.run(run(args.base_url, args.api_key, args.model))


if __name__ == "__main__":
    main()
