"""Provider selection and the resilience wrapper around it.

Two guarantees hold for every caller:

1. `get_llm_provider()` always returns something usable. With no API key
   configured it returns the deterministic parser.
2. `extract_requirement()` never raises because of the AI. A provider failure
   degrades to the deterministic parser and is reported as `used_fallback`, so
   the business workflow continues (AI_ARCHITECTURE.md section 7).
"""

from __future__ import annotations

import time

from app.ai.base import ExtractionResult, LLMProvider
from app.ai.providers.null_provider import NullLLMProvider, parse_requirement_text
from app.core.config import AIProvider, settings
from app.core.logging import get_logger

logger = get_logger("ai.registry")

_provider: LLMProvider | None = None


class CircuitBreaker:
    """Stops hammering a provider that is already failing."""

    def __init__(self, threshold: int, cooldown_seconds: int) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown_seconds:
            self.reset()
            return False
        return True

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold and self.opened_at is None:
            self.opened_at = time.monotonic()
            logger.warning("ai_circuit_opened", cooldown_seconds=self.cooldown_seconds)

    def record_success(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.failures = 0
        self.opened_at = None


_breaker = CircuitBreaker(
    settings.AI_CIRCUIT_BREAKER_FAILURES, settings.AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS
)


def get_llm_provider() -> LLMProvider:
    """Resolve the configured provider, falling back to deterministic parsing."""
    global _provider
    if _provider is not None:
        return _provider

    if settings.LLM_PROVIDER is AIProvider.ANTHROPIC and settings.LLM_API_KEY:
        try:
            from app.ai.providers.anthropic_provider import AnthropicProvider

            _provider = AnthropicProvider(settings.LLM_API_KEY, settings.LLM_MODEL)
            logger.info("ai_provider_selected", provider="anthropic", model=settings.LLM_MODEL)
            return _provider
        except Exception as exc:  # pragma: no cover - only on a bad install/config
            logger.error("ai_provider_init_failed", provider="anthropic", error=str(exc))

    if settings.LLM_PROVIDER is not AIProvider.NULL and not settings.LLM_API_KEY:
        logger.warning("ai_provider_missing_key", configured=settings.LLM_PROVIDER.value)

    _provider = NullLLMProvider()
    return _provider


def reset_provider() -> None:
    """Used by tests and by a configuration change at runtime."""
    global _provider
    _provider = None
    _breaker.reset()


async def extract_requirement(text: str) -> ExtractionResult:
    """Extract with the configured provider; never raise because of the AI."""
    provider = get_llm_provider()

    if isinstance(provider, NullLLMProvider):
        return parse_requirement_text(text)

    if _breaker.is_open:
        logger.warning("ai_circuit_open_using_fallback")
        result = parse_requirement_text(text)
        result.used_fallback = True
        result.warnings.append(
            "AI extraction is temporarily unavailable; fields were filled by the rule-based parser."
        )
        return result

    last_error: Exception | None = None
    for attempt in range(1, settings.LLM_MAX_RETRIES + 1):
        try:
            result = await provider.extract_requirement(text)
            _breaker.record_success()
            return result
        except Exception as exc:
            last_error = exc
            logger.warning(
                "ai_extraction_attempt_failed",
                attempt=attempt,
                max_attempts=settings.LLM_MAX_RETRIES,
                error=str(exc),
            )

    _breaker.record_failure()
    logger.error("ai_extraction_failed_using_fallback", error=str(last_error))

    result = parse_requirement_text(text)
    result.used_fallback = True
    result.warnings.append(
        "AI extraction did not succeed; fields were filled by the rule-based parser. "
        "Review them before accepting."
    )
    return result


__all__ = ["CircuitBreaker", "extract_requirement", "get_llm_provider", "reset_provider"]
