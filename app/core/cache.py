"""A small key/value cache with a time to live.

Mirrors the rate limiter: an in-process dictionary by default, Redis when one
is configured. The fallback is honest rather than silent — a single API process
gets correct behaviour, and several processes each keep their own copy, which
for cached search results means paying for the same query more than once rather
than returning anything wrong.

Deliberately not a general caching layer. It exists because a job search run
costs money and takes about thirty seconds, so asking the same question twice
should be free.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import CacheBackend, settings
from app.core.logging import get_logger

logger = get_logger("cache")


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float


class InMemoryCache:
    """Process-local. Correct for a single API process."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    async def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry.value

    async def set(self, key: str, value: Any, *, ttl: int) -> None:
        self._entries[key] = _Entry(value=value, expires_at=time.monotonic() + ttl)

        # Opportunistic sweep so a long-lived process does not grow unbounded.
        if len(self._entries) > 5_000:
            now = time.monotonic()
            self._entries = {k: v for k, v in self._entries.items() if v.expires_at > now}

    async def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


class RedisCache:
    """Shared, so several API processes see one another's entries."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # imported lazily: optional dependency

        self._client = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(key)
        except Exception as exc:  # pragma: no cover - only when Redis dies
            # A cache outage must never take the request with it. A miss is
            # slower and costs a run; an exception would be a failure.
            logger.warning("cache_backend_unavailable", error=str(exc))
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, *, ttl: int) -> None:
        try:
            await self._client.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:  # pragma: no cover
            logger.warning("cache_backend_unavailable", error=str(exc))

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except Exception as exc:  # pragma: no cover
            logger.warning("cache_backend_unavailable", error=str(exc))


_cache: InMemoryCache | RedisCache | None = None


def get_cache() -> InMemoryCache | RedisCache:
    global _cache
    if _cache is None:
        if settings.CACHE_BACKEND is CacheBackend.REDIS:
            _cache = RedisCache(settings.REDIS_URL)
        else:
            _cache = InMemoryCache()
    return _cache


def reset_cache() -> None:
    """Used by the test suite between cases."""
    global _cache
    if isinstance(_cache, InMemoryCache):
        _cache.clear()
    _cache = None


__all__ = ["InMemoryCache", "RedisCache", "get_cache", "reset_cache"]
