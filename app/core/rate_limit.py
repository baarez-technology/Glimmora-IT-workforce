"""Rate limiting (SECURITY.md section 8).

Fixed-window counters. Redis-backed when configured, in-process otherwise, so
the protection exists on the no-infrastructure path too — a login endpoint that
is only rate limited "in production" is not rate limited.

This is distinct from account lockout: lockout protects one account from
password guessing, rate limiting protects the endpoint from volume.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from app.core.config import CacheBackend, settings
from app.core.errors import RateLimitedError
from app.core.logging import get_logger

logger = get_logger("ratelimit")


@dataclass
class _Window:
    count: int
    resets_at: float


class InMemoryRateLimiter:
    """Process-local counters. Correct for a single API process."""

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}

    def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        window = self._windows.get(key)

        if window is None or window.resets_at <= now:
            window = _Window(count=0, resets_at=now + window_seconds)
            self._windows[key] = window

        window.count += 1
        retry_after = max(1, int(window.resets_at - now))

        # Opportunistic sweep so a long-lived process does not accumulate keys.
        if len(self._windows) > 10_000:
            self._windows = {k: v for k, v in self._windows.items() if v.resets_at > now}

        return window.count <= limit, retry_after

    def reset(self) -> None:
        self._windows.clear()


class RedisRateLimiter:
    """Shared counters, so limits hold across multiple API processes."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # imported lazily: optional dependency

        self._client = redis.from_url(url, decode_responses=True)

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        try:
            pipe = self._client.pipeline()
            pipe.incr(key)
            pipe.ttl(key)
            count, ttl = await pipe.execute()
            if ttl < 0:
                await self._client.expire(key, window_seconds)
                ttl = window_seconds
            return int(count) <= limit, max(1, int(ttl))
        except Exception as exc:  # pragma: no cover - only when Redis dies mid-flight
            # Never let a rate limiter outage take the API down with it.
            logger.warning("rate_limit_backend_unavailable", error=str(exc))
            return True, 0


_limiter: InMemoryRateLimiter | RedisRateLimiter | None = None


def get_limiter() -> InMemoryRateLimiter | RedisRateLimiter:
    global _limiter
    if _limiter is None:
        if settings.CACHE_BACKEND is CacheBackend.REDIS:
            _limiter = RedisRateLimiter(settings.REDIS_URL)
        else:
            _limiter = InMemoryRateLimiter()
    return _limiter


def reset_limiter() -> None:
    """Used by the test suite between cases."""
    global _limiter
    if isinstance(_limiter, InMemoryRateLimiter):
        _limiter.reset()
    _limiter = None


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(
    scope: str,
    *,
    limit: int | None = None,
    limit_setting: str | None = None,
    window_seconds: int = 60,
) -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency factory.

        dependencies=[Depends(rate_limit("login", limit_setting="RATE_LIMIT_LOGIN_PER_MINUTE"))]

    Prefer `limit_setting` over a literal `limit`: the setting is read per
    request, so the limit is not frozen at import time and stays configurable.
    """

    async def guard(request: Request) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return

        if limit is not None:
            effective_limit = limit
        elif limit_setting is not None:
            effective_limit = int(getattr(settings, limit_setting))
        else:
            effective_limit = settings.RATE_LIMIT_DEFAULT_PER_MINUTE
        key = f"ratelimit:{scope}:{_client_key(request)}"

        limiter = get_limiter()
        result = limiter.hit(key, limit=effective_limit, window_seconds=window_seconds)
        allowed, retry_after = await result if hasattr(result, "__await__") else result  # type: ignore[misc]

        if not allowed:
            logger.warning("rate_limited", scope=scope, path=request.url.path)
            raise RateLimitedError(
                headers={"Retry-After": str(retry_after)},
                log_detail=f"scope={scope} limit={effective_limit}/{window_seconds}s",
            )

    return guard


__all__ = [
    "InMemoryRateLimiter",
    "RedisRateLimiter",
    "get_limiter",
    "rate_limit",
    "reset_limiter",
]
