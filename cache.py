"""TTL cache for extracted data.

Why this exists: if Claude calls extract_table on the same page twice in a row,
re-rendering the page is wasteful and adds detection risk (more requests).
TTLCache returns the previous result if it's fresh, transparently.

Analogy: like a fridge with use-by dates. If the milk is still good, drink it;
if expired, throw it out and buy new.
"""
from __future__ import annotations

import hashlib
from typing import Any

from cachetools import TTLCache

from .logging_config import get_logger

log = get_logger("cache")


class ExtractionCache:
    def __init__(self, ttl_seconds: int = 60, max_entries: int = 128, enabled: bool = True):
        self.enabled = enabled and ttl_seconds > 0
        self._cache: TTLCache = TTLCache(maxsize=max_entries, ttl=ttl_seconds)

    @staticmethod
    def make_key(*parts: Any) -> str:
        """Stable hash of arbitrary parts so callers don't worry about key format."""
        h = hashlib.sha256()
        for p in parts:
            h.update(repr(p).encode("utf-8"))
            h.update(b"|")
        return h.hexdigest()[:32]

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        hit = self._cache.get(key)
        if hit is not None:
            log.info("cache_hit", key=key)
        return hit

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self._cache[key] = value
        log.info("cache_set", key=key, size=len(self._cache))

    def clear(self) -> None:
        self._cache.clear()
        log.info("cache_cleared")
