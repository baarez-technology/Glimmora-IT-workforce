"""Anthropic-backed extraction.

The model is asked for structured JSON only. It never sees a rate card, a cost
or a margin unless the operator explicitly opts in, and it never produces a
score — its output is a set of candidate field values that a human then reviews
(AI_ARCHITECTURE.md sections 1 and 8).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from app.ai.base import ExtractionResult, FieldExtraction
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("ai.anthropic")

PROMPT_VERSION = "jd-extract-v1"

EXTRACTION_PROMPT = """You extract structured data from IT staffing job descriptions.

Return ONLY a JSON object. No prose, no markdown fence.

For each field return an object:
{"value": <value or null>, "confidence": <0..1>,
 "evidence": "<the exact substring of the source that supports it, or null>"}

Fields:
- title (string)
- role (string, e.g. "SAP Consultant", "Java Developer")
- mandatory_skills (array of strings)
- preferred_skills (array of strings)
- technologies (array of strings)
- experience_min_years (integer)
- experience_max_years (integer)
- duration_months (integer)
- positions (integer)
- location (string)
- country (2-letter ISO code)
- work_mode (one of ONSITE, HYBRID, REMOTE)
- contract_type (one of CONTRACT, CONTRACT_TO_HIRE, PERMANENT, OUTSOURCED_SERVICE)
- rate_min (number)
- rate_max (number)
- rate_currency (3-letter ISO code)
- rate_unit (one of HOURLY, DAILY, MONTHLY, ANNUAL)
- response_deadline_at (ISO 8601 datetime)
- start_by_date (ISO 8601 date)
- availability_requirement (string, e.g. notice period)
- customer_name (string)
- project_name (string)

Rules:
- If a field is not stated, return null with confidence 0. Never guess.
- "evidence" must be copied verbatim from the source text.
- Do not infer a rate, a deadline or a client name that is not written down.

Job description:
---
{text}
---"""

#: Money and dates are never auto-accepted, so their confidence is capped below
#: the review threshold no matter how certain the model claims to be.
_CAPPED_FIELDS = frozenset(
    {
        "rate_min",
        "rate_max",
        "rate_currency",
        "rate_unit",
        "response_deadline_at",
        "start_by_date",
    }
)
_CAP = 0.8

_EXPECTED_FIELDS = (
    "title",
    "role",
    "mandatory_skills",
    "preferred_skills",
    "technologies",
    "experience_min_years",
    "experience_max_years",
    "duration_months",
    "positions",
    "location",
    "country",
    "work_mode",
    "contract_type",
    "rate_min",
    "rate_max",
    "rate_currency",
    "rate_unit",
    "response_deadline_at",
    "start_by_date",
    "availability_requirement",
    "customer_name",
    "project_name",
)


def _text_of(response: Any) -> str:
    """Concatenate the text blocks of a response.

    A response can carry thinking, tool-use and container blocks alongside text,
    so blocks are read defensively rather than by index.
    """
    return "".join(
        getattr(block, "text", "")
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model_id: str) -> None:
        from anthropic import AsyncAnthropic

        self.model_id = model_id
        self._client = AsyncAnthropic(api_key=api_key, timeout=settings.LLM_TIMEOUT_SECONDS)

    async def extract_requirement(self, text: str) -> ExtractionResult:
        started = time.perf_counter()
        response = await self._client.messages.create(
            model=self.model_id,
            max_tokens=4096,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT.format(text=text[:40_000])}],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        payload = _extract_json(_text_of(response))
        if payload is None:
            raise ValueError("The model did not return parsable JSON")

        fields = _to_fields(payload, source=text)
        present = [field for field in fields.values() if field.is_present]
        overall = round(sum(f.confidence for f in present) / len(present), 3) if present else 0.0

        usage = getattr(response, "usage", None)
        return ExtractionResult(
            fields=fields,
            overall_confidence=overall,
            provider=self.name,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            token_usage=(
                {"input": usage.input_tokens, "output": usage.output_tokens} if usage else None
            ),
        )

    async def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        response = await self._client.messages.create(
            model=self.model_id,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return _text_of(response)


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Tolerate a fenced or prefixed response without accepting nonsense."""
    candidate = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = candidate[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _to_fields(payload: dict[str, Any], *, source: str) -> dict[str, FieldExtraction]:
    fields: dict[str, FieldExtraction] = {}

    for name in _EXPECTED_FIELDS:
        entry = payload.get(name)
        if not isinstance(entry, dict):
            # Accept a bare value too — models sometimes flatten the shape.
            entry = {"value": entry, "confidence": 0.6 if entry is not None else 0.0}

        value = entry.get("value")
        try:
            confidence = float(entry.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if name in _CAPPED_FIELDS:
            confidence = min(confidence, _CAP)

        evidence = entry.get("evidence")
        start = end = None
        if isinstance(evidence, str) and evidence.strip():
            # Verify the quote actually appears in the source. A model that
            # paraphrases its own evidence is a model inventing support.
            index = source.find(evidence.strip())
            if index >= 0:
                start, end = index, index + len(evidence.strip())
            else:
                evidence = None
                confidence = min(confidence, 0.5)
        else:
            evidence = None

        fields[name] = FieldExtraction(
            value=value,
            confidence=confidence,
            evidence=evidence,
            evidence_start=start,
            evidence_end=end,
        )

    return fields


__all__ = ["EXTRACTION_PROMPT", "PROMPT_VERSION", "AnthropicProvider"]
