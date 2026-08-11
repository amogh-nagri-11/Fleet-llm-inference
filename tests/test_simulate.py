import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import simulate  # noqa: E402


# ── percentile ────────────────────────────────────────────────

def test_percentile_p50_of_odd_list():
    assert simulate.percentile([1, 2, 3], 50) == 2


def test_percentile_empty_list():
    assert simulate.percentile([], 50) == 0.0


def test_percentile_p100_is_max():
    assert simulate.percentile([1, 2, 3, 4], 100) == 4


# ── make_prompt ───────────────────────────────────────────────

@pytest.mark.parametrize("name", ["coding", "research", "batch"])
def test_make_prompt_respects_length_bounds(name):
    profile = simulate.PROFILES[name]
    for _ in range(20):
        prompt = simulate.make_prompt(profile)
        assert profile.prompt_lengths[0] <= len(prompt) <= profile.prompt_lengths[1]


# ── assign_workloads ─────────────────────────────────────────

def test_assign_workloads_single_type():
    assert simulate.assign_workloads(5, "coding") == ["coding"] * 5


def test_assign_workloads_mixed_only_uses_known_profiles():
    assigned = simulate.assign_workloads(50, "mixed")
    assert len(assigned) == 50
    assert set(assigned) <= set(simulate.PROFILES)


def test_assign_workloads_mixed_uses_more_than_one_profile_at_scale():
    # Statistically near-certain with 100 agents across 3 profiles.
    assigned = simulate.assign_workloads(100, "mixed")
    assert len(set(assigned)) > 1


# ── run_agent (fake client, no network) ────────────────────────

class FakeClient:
    def __init__(self, fail_after: int = None):
        self.calls = []
        self.fail_after = fail_after

    async def chat(self, messages, model, agent_id, workflow_id):
        self.calls.append({
            "agent_id": agent_id, "workflow_id": workflow_id, "model": model,
            "prompt": messages[0]["content"],
        })
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("simulated failure")
        return {"message": {"content": "ok"}}


@pytest.mark.asyncio
async def test_run_agent_records_successful_results():
    client = FakeClient()
    results = simulate.SimResults()
    # think_time for "batch" is near-zero, so this completes quickly.
    end_time = time.time() + 0.3

    await simulate.run_agent(0, "batch", client, "llama3:latest", end_time, results, start_delay=0)

    assert len(results.results) > 0
    assert all(r.ok for r in results.results)
    assert all(r.workload == "batch" for r in results.results)


@pytest.mark.asyncio
async def test_run_agent_uses_consistent_agent_and_workflow_id():
    client = FakeClient()
    results = simulate.SimResults()
    end_time = time.time() + 0.3

    await simulate.run_agent(3, "coding", client, "llama3:latest", end_time, results, start_delay=0)

    assert len(client.calls) > 0
    agent_ids = {c["agent_id"] for c in client.calls}
    workflow_ids = {c["workflow_id"] for c in client.calls}
    assert agent_ids == {"coding-agent-3"}
    assert len(workflow_ids) == 1  # same workflow across the whole run


@pytest.mark.asyncio
async def test_run_agent_records_failures_without_raising():
    client = FakeClient(fail_after=0)  # every call fails
    results = simulate.SimResults()
    end_time = time.time() + 0.3

    await simulate.run_agent(0, "batch", client, "llama3:latest", end_time, results, start_delay=0)

    assert len(results.results) > 0
    assert all(not r.ok for r in results.results)


@pytest.mark.asyncio
async def test_run_agent_respects_start_delay():
    client = FakeClient()
    results = simulate.SimResults()
    end_time = time.time() + 0.05  # already-short window

    start = time.perf_counter()
    await simulate.run_agent(0, "batch", client, "llama3:latest", end_time, results, start_delay=0.2)
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.2
    assert results.results == []  # end_time already passed by the time it started
