import threading

import pytest

from marketpulse.core.ratelimit import RateLimiter


class FakeClock:
    """Deterministic clock; sleeping advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_allows_burst_up_to_capacity_without_waiting() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 1.0, clock=clock, sleep=clock.sleep)
    assert [limiter.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert clock.sleeps == []


def test_waits_once_capacity_is_exhausted() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 1.0, clock=clock, sleep=clock.sleep)  # 2 per second
    limiter.acquire()
    limiter.acquire()
    assert limiter.acquire() == pytest.approx(0.5)
    assert limiter.acquire() == pytest.approx(0.5)


def test_refills_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 1.0, clock=clock, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    clock.now += 10.0  # long idle: refills to capacity, not beyond
    assert [limiter.acquire() for _ in range(2)] == [0.0, 0.0]
    assert limiter.acquire() > 0


def test_sustained_rate_is_respected() -> None:
    clock = FakeClock()
    limiter = RateLimiter(120, 60.0, clock=clock, sleep=clock.sleep)  # FRED-like: 120/min
    for _ in range(360):
        limiter.acquire()
    # 120 burst + 240 more at 2/s => at least 120 seconds elapsed
    assert clock.now == pytest.approx(120.0)


def test_concurrent_callers_never_exceed_rate() -> None:
    clock = FakeClock()
    lock = threading.Lock()
    limiter = RateLimiter(5, 1.0, clock=clock, sleep=lambda s: None)
    waits: list[float] = []

    def worker() -> None:
        wait = limiter.acquire()
        with lock:
            waits.append(wait)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 5 immediate, then each further caller reserved a distinct, later slot (0.2s apart).
    assert sorted(waits) == pytest.approx([0.0] * 5 + [0.2 * i for i in range(1, 16)])


@pytest.mark.parametrize(("calls", "period"), [(0, 1.0), (1, 0.0), (1, -1.0)])
def test_rejects_invalid_configuration(calls: int, period: float) -> None:
    with pytest.raises(ValueError):
        RateLimiter(calls, period)
