import time

from router.circuit_breaker import CircuitBreaker, CircuitState


def make_breaker(threshold=3, timeout=0.1) -> CircuitBreaker:
    breaker = CircuitBreaker("http://worker")
    breaker.threshold = threshold
    breaker.timeout = timeout
    return breaker


def test_starts_closed():
    breaker = make_breaker()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.is_open() is False


def test_opens_after_threshold_failures():
    breaker = make_breaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False  # below threshold

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.is_open() is True


def test_success_resets_failure_count():
    breaker = make_breaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count == 0
    assert breaker.state == CircuitState.CLOSED


def test_transitions_to_half_open_after_cooldown():
    breaker = make_breaker(threshold=1, timeout=0.05)
    breaker.record_failure()
    assert breaker.is_open() is True

    time.sleep(0.06)
    assert breaker.is_open() is False
    assert breaker.state == CircuitState.HALF_OPEN


def test_half_open_failure_reopens_immediately():
    breaker = make_breaker(threshold=1, timeout=0.05)
    breaker.record_failure()
    time.sleep(0.06)
    breaker.is_open()  # flips to HALF_OPEN
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.is_open() is True


def test_half_open_success_closes():
    breaker = make_breaker(threshold=1, timeout=0.05)
    breaker.record_failure()
    time.sleep(0.06)
    breaker.is_open()  # flips to HALF_OPEN
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0
