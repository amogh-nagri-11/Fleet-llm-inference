import pytest

from router.load_balancer import LoadBalancer
from config import settings


def test_round_robin_cycles_through_workers():
    settings.ROUTING_STRATEGY = "round_robin"
    lb = LoadBalancer(["http://a", "http://b", "http://c"])

    picked = [lb.pick_worker().stats.url for _ in range(6)]
    assert picked == ["http://a", "http://b", "http://c"] * 2


def test_least_latency_picks_lowest_avg_latency():
    settings.ROUTING_STRATEGY = "least_latency"
    lb = LoadBalancer(["http://a", "http://b"])

    fast, slow = lb.workers
    fast.stats.total_requests = 1
    fast.stats.total_latency = 0.1
    slow.stats.total_requests = 1
    slow.stats.total_latency = 5.0

    assert lb.pick_worker().stats.url == "http://a"


def test_queue_depth_picks_least_active_requests():
    settings.ROUTING_STRATEGY = "queue_depth"
    lb = LoadBalancer(["http://a", "http://b"])

    busy, idle = lb.workers
    busy.stats.active_requests = 5
    idle.stats.active_requests = 0

    assert lb.pick_worker().stats.url == "http://b"


def test_raises_when_no_workers_available():
    lb = LoadBalancer(["http://a"])
    lb.workers[0].stats.is_healthy = False

    with pytest.raises(RuntimeError):
        lb.pick_worker()


def test_open_circuit_excludes_worker():
    lb = LoadBalancer(["http://a", "http://b"])
    settings.ROUTING_STRATEGY = "round_robin"

    breaker = lb.breakers["http://a"]
    breaker.threshold = 1
    breaker.record_failure()
    assert breaker.is_open() is True

    for _ in range(4):
        assert lb.pick_worker().stats.url == "http://b"


def test_add_worker_is_idempotent():
    lb = LoadBalancer(["http://a"])
    assert lb.add_worker("http://b") is True
    assert lb.add_worker("http://b") is False
    assert lb.has_worker("http://b") is True


def test_remove_worker():
    lb = LoadBalancer(["http://a", "http://b"])
    assert lb.remove_worker("http://a") is True
    assert lb.has_worker("http://a") is False
    assert lb.remove_worker("http://missing") is False


def test_add_worker_uses_configured_timeout():
    settings.REQUEST_TIMEOUT = 42
    lb = LoadBalancer(["http://a"])
    lb.add_worker("http://b")
    added = next(w for w in lb.workers if w.stats.url == "http://b")
    assert added.timeout == 42


# ── Context-aware routing (Phase 9) ───────────────────────────

def test_worker_defaults_max_context_tokens_from_settings():
    settings.WORKER_MAX_CONTEXT_TOKENS = 8192
    lb = LoadBalancer(["http://a"])
    assert lb.workers[0].max_context_tokens == 8192


def test_no_context_tokens_arg_behaves_identically_to_before_phase_9():
    settings.ROUTING_STRATEGY = "round_robin"
    lb = LoadBalancer(["http://a", "http://b"])
    picked = [lb.pick_worker().stats.url for _ in range(4)]
    assert picked == ["http://a", "http://b"] * 2


def test_context_tokens_excludes_worker_with_insufficient_capacity():
    lb = LoadBalancer(["http://small", "http://big"])
    small, big = lb.workers
    small.max_context_tokens = 4096
    big.max_context_tokens = 32768

    for _ in range(3):
        assert lb.pick_worker(context_tokens=16000).stats.url == "http://big"


def test_context_tokens_raises_specific_error_when_none_have_capacity():
    lb = LoadBalancer(["http://a", "http://b"])
    for w in lb.workers:
        w.max_context_tokens = 4096

    with pytest.raises(RuntimeError, match="context"):
        lb.pick_worker(context_tokens=16000)


def test_context_tokens_still_respects_health_and_circuit_state():
    lb = LoadBalancer(["http://a", "http://b"])
    lb.workers[0].stats.is_healthy = False
    lb.workers[1].max_context_tokens = 32768

    assert lb.pick_worker(context_tokens=16000).stats.url == "http://b"


def test_context_aware_filtering_does_not_exclude_merely_busy_workers():
    """REDESIGN.md §41: a worker that's heavily loaded but has capacity
    stays eligible — filtering is on hard capacity only, not load."""
    settings.ROUTING_STRATEGY = "queue_depth"
    lb = LoadBalancer(["http://busy", "http://idle"])
    busy, idle = lb.workers
    busy.stats.active_requests = 100
    idle.stats.active_requests = 0
    busy.max_context_tokens = 32768
    idle.max_context_tokens = 32768

    # Both eligible on capacity; queue_depth strategy still picks the idle one.
    assert lb.pick_worker(context_tokens=16000).stats.url == "http://idle"
