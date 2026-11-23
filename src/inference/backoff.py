"""Exponential backoff with deterministic hash-based jitter."""

from __future__ import annotations

from hashlib import sha256

from src.core.config import BackoffConfig


class BackoffPolicy:
    def __init__(self, config: BackoffConfig) -> None:
        self._config = config

    def delay(self, attempt: int) -> float:
        """Delay before the next attempt; attempt 0 is the first retry delay."""

        exponent = max(0, attempt)
        raw = min(
            self._config.max_seconds,
            self._config.base_seconds * float(2**exponent),
        )
        jitter = min(1.0, max(0.0, self._config.jitter_fraction))
        if jitter > 0.0:
            digest = sha256(f"{self._config.seed}:{exponent}".encode()).digest()
            fraction = int.from_bytes(digest[:8], "big") / float(2**64)
            raw *= 1.0 + ((fraction * 2.0) - 1.0) * jitter
        return max(0.0, min(self._config.max_seconds, raw))
