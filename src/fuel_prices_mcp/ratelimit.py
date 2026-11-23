"""Per-principal token-bucket rate limiting with bounded memory."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Classic token bucket: ``burst`` capacity refilled at ``rate_per_minute``.

    State is kept per principal in an LRU map capped at ``max_keys`` entries so a
    flood of distinct identities cannot exhaust memory. Not thread-safe; it is
    used from a single event loop and never awaits while mutating state.
    """

    def __init__(
        self,
        *,
        rate_per_minute: int,
        burst: int,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_minute <= 0 or burst <= 0:
            raise ValueError("rate_per_minute and burst must be positive")
        self._rate = rate_per_minute / 60.0
        self._burst = float(burst)
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def acquire(self, key: str) -> float:
        """Consume one token for ``key``. Returns 0 on success, else seconds until a token is available."""
        now = self._clock()
        bucket = self._buckets.pop(key, None)
        if bucket is None:
            bucket = _Bucket(tokens=self._burst, updated=now)
        else:
            bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.updated) * self._rate)
            bucket.updated = now
        self._buckets[key] = bucket
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / self._rate
