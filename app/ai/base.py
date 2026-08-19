"""AI provider contracts (AI_ARCHITECTURE.md section 2).

Two rules hold everywhere below this module:

1. The LLM never produces a number that reaches the database as a score.
2. Anything a provider extracted is tagged with its provenance and confidence,
   and is not business data until a human accepts it (AD-7).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class FieldExtraction(BaseModel):
    """One extracted field, with everything a reviewer needs to verify it.

    `evidence` is the exact span of source text the value came from, and
    `evidence_start`/`evidence_end` are character offsets into that source, so
    the review screen can highlight it. A reviewer who can see where a value came
    from verifies it; one who cannot merely trusts it.
    """

    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str | None = None
    evidence_start: int | None = None
    evidence_end: int | None = None

    @property
    def is_present(self) -> bool:
        if self.value is None:
            return False
        return not (isinstance(self.value, (list, str)) and len(self.value) == 0)


class ExtractionResult(BaseModel):
    """Everything a parse produced, plus how it was produced."""

    fields: dict[str, FieldExtraction] = Field(default_factory=dict)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    provider: str = "null"
    model_id: str = "deterministic"
    prompt_version: str | None = None
    latency_ms: int | None = None
    token_usage: dict[str, int] | None = None
    warnings: list[str] = Field(default_factory=list)
    #: True when the primary provider failed and the fallback produced this.
    used_fallback: bool = False

    def get(self, name: str) -> FieldExtraction:
        return self.fields.get(name, FieldExtraction())

    def value(self, name: str, default: Any = None) -> Any:
        field = self.fields.get(name)
        return field.value if field and field.is_present else default

    def confident_fields(self, threshold: float) -> list[str]:
        return sorted(
            name
            for name, field in self.fields.items()
            if field.is_present and field.confidence >= threshold
        )

    def uncertain_fields(self, threshold: float) -> list[str]:
        return sorted(
            name
            for name, field in self.fields.items()
            if field.is_present and field.confidence < threshold
        )


@runtime_checkable
class LLMProvider(Protocol):
    """Structured extraction from unstructured text."""

    name: str
    model_id: str

    async def extract_requirement(self, text: str) -> ExtractionResult: ...

    async def complete(self, prompt: str, *, max_tokens: int = 1024) -> str: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Vector embeddings. Implemented in Phase 6, declared here for one contract."""

    name: str
    model_id: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


__all__ = [
    "EmbeddingProvider",
    "ExtractionResult",
    "FieldExtraction",
    "LLMProvider",
]
