import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks" / "experiments"))

import context_aware_routing as exp  # noqa: E402


def test_all_scenarios_match_expectation():
    results = exp.run()
    assert all(r["matches_expectation"] for r in results)


def test_small_request_all_workers_eligible():
    results = exp.run()
    small = next(r for r in results if "small" in r["scenario"])
    assert len(small["eligible"]) == 3
    assert small["rejected"] is False


def test_medium_request_excludes_low_capacity_worker():
    results = exp.run()
    medium = next(r for r in results if "medium" in r["scenario"])
    assert "http://worker-b" not in medium["eligible"]
    assert "http://worker-a" in medium["eligible"]
    assert "http://worker-c" in medium["eligible"]


def test_oversized_request_rejected():
    results = exp.run()
    oversized = next(r for r in results if "oversized" in r["scenario"])
    assert oversized["eligible"] == []
    assert oversized["rejected"] is True


def test_busy_worker_stays_eligible_when_it_has_capacity():
    """§41: a heavily-loaded worker with enough capacity must still be
    eligible — filtering is on hard capacity only, not load."""
    results = exp.run()
    medium = next(r for r in results if "medium" in r["scenario"])
    assert "http://worker-c" in medium["eligible"]  # loaded, but 32k capacity
