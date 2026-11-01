"""Thread-safe token-bucket rate limiter.

Allows bursts of up to `max_calls`, refilling at `max_calls / period_seconds` tokens per second.
Callers reserve a token under a lock and then sleep outside it, so concurrent callers are
spaced correctly without holding the lock while waiting.
"""

import threading
import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        max_calls: int,
        period_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if period_seconds <= 0:
            raise ValueError("period_seconds must be positive")
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._rate = max_calls / period_seconds
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(max_calls)
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a call is allowed. Returns the number of seconds waited."""
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._updated)
            self._tokens = min(float(self.max_calls), self._tokens + elapsed * self._rate)
            self._updated = now
            self._tokens -= 1.0
            wait = 0.0 if self._tokens >= 0 else -self._tokens / self._rate
        if wait > 0:
            self._sleep(wait)
        return wait
