"""Deterministic JD parser — the fallback that needs no API key.

This is not a toy. `LLM_PROVIDER=null` is the default, so for an offline
deployment this *is* the parser. It works by pattern matching against the
vocabulary in `app.ai.vocabulary`, and it reports honest confidence: a value
found by an unambiguous labelled pattern ("Location: Doha") scores high, one
inferred from loose prose scores low, and anything it cannot find is left absent
rather than guessed.

Confidence is deliberately capped below 0.85 for money and date fields, because
the review policy never auto-accepts those (AI_ARCHITECTURE.md section 3).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.ai.base import ExtractionResult, FieldExtraction
from app.ai.vocabulary import (
    CURRENCY_TOKENS,
    LOCATION_COUNTRIES,
    ROLE_KEYWORDS,
    SKILL_TO_TECHNOLOGY,
    SKILL_VOCABULARY,
    build_alias_index,
)

_ALIAS_INDEX = build_alias_index()
#: Longest aliases first so "sap fico" beats "sap".
_ALIAS_ORDER = sorted(_ALIAS_INDEX, key=len, reverse=True)

_LABEL = r"(?:^|\n)\s*(?:{labels})\s*[:\-–]\s*(?P<value>[^\n]+)"

#: Clause boundaries for inline preference detection.
#:
#: Sentence punctuation, a blank line, or the start of a bullet — but NOT every
#: newline. Job descriptions wrap mid-sentence, and treating a wrapped line as a
#: new clause would separate "Oracle EBS experience is" from "nice to have".
_CLAUSE_BREAK = re.compile(r"[.;:!?]|\n\s*\n|\n\s*[-*•]|\n\s*\d+[.)]")


def _labelled(text: str, labels: list[str]) -> re.Match[str] | None:
    pattern = _LABEL.format(labels="|".join(re.escape(label) for label in labels))
    return re.search(pattern, text, flags=re.IGNORECASE)


def _span(match: re.Match[str], group: str | int = 0) -> tuple[str, int, int]:
    return match.group(group), match.start(group), match.end(group)


def _field(
    value: object,
    confidence: float,
    match: re.Match[str] | None = None,
    group: str | int = 0,
) -> FieldExtraction:
    if match is None:
        return FieldExtraction(value=value, confidence=confidence)
    evidence, start, end = _span(match, group)
    return FieldExtraction(
        value=value,
        confidence=confidence,
        evidence=evidence.strip()[:240],
        evidence_start=start,
        evidence_end=end,
    )


# ---------------------------------------------------------------- extractors


def extract_title(text: str) -> FieldExtraction:
    match = _labelled(
        text, ["job title", "position title", "position", "role title", "title", "role"]
    )
    if match:
        value = match.group("value").strip(" .;")
        if 2 < len(value) <= 240:
            return _field(value, 0.92, match, "value")

    # Fall back to the first substantial line, which is where a title usually is.
    for line in text.splitlines():
        candidate = line.strip(" #*-•\t")
        if 4 < len(candidate) <= 120 and not candidate.endswith(":"):
            start = text.find(candidate)
            return FieldExtraction(
                value=candidate,
                confidence=0.55,
                evidence=candidate,
                evidence_start=start if start >= 0 else None,
                evidence_end=(start + len(candidate)) if start >= 0 else None,
            )
    return FieldExtraction()


def extract_role(text: str, title: str | None) -> FieldExtraction:
    haystack = f"{title or ''}\n{text}".lower()
    for role, keywords in ROLE_KEYWORDS:
        for keyword in keywords:
            index = haystack.find(keyword)
            if index >= 0:
                # High confidence only when the role appears in the title itself.
                in_title = bool(title) and keyword in (title or "").lower()
                return FieldExtraction(
                    value=role,
                    confidence=0.9 if in_title else 0.7,
                    evidence=keyword,
                )
    return FieldExtraction()


def extract_skills(text: str) -> tuple[FieldExtraction, FieldExtraction, FieldExtraction]:
    """Return (mandatory, preferred, technologies).

    Skills named under a "preferred"/"nice to have" heading are classified as
    preferred; everything else found is mandatory. Getting this wrong in the
    permissive direction is safer: the matching engine penalises a missing
    mandatory skill heavily, so over-classifying as mandatory would hide
    reasonable candidates.
    """
    lowered = text.lower()

    preference_markers = (
        "preferred",
        "preferably",
        "nice to have",
        "nice-to-have",
        "desirable",
        "advantageous",
        "good to have",
        "would be a plus",
        "is a plus",
        "bonus",
        "optional",
    )

    # Two phrasings both mean "preferred", and they point in opposite directions:
    #   heading style - "Nice to have:" then a bulleted list (marker precedes)
    #   inline style  — "Oracle EBS experience is nice to have" (marker follows)
    # Heading zones cover the first; sentence scanning covers the second.
    preferred_zones: list[tuple[int, int]] = []
    for marker in preference_markers:
        for match in re.finditer(re.escape(marker), lowered):
            trailer = lowered[match.end() : match.end() + 3]
            is_heading = trailer.lstrip().startswith((":", "-", "\n")) or trailer.strip() == ""
            if is_heading:
                # Only a heading governs the block after it. An inline marker
                # ("X is nice to have") governs its own clause and nothing more,
                # or it would swallow the next requirement in the list.
                preferred_zones.append((match.end(), min(len(lowered), match.end() + 400)))

    boundaries = [0, *(m.end() for m in _CLAUSE_BREAK.finditer(lowered)), len(lowered)]

    def enclosing_clause(position: int) -> str:
        start = max((b for b in boundaries if b <= position), default=0)
        end = min((b for b in boundaries if b > position), default=len(lowered))
        return lowered[start:end]

    def in_preferred_zone(position: int) -> bool:
        if any(start <= position < end for start, end in preferred_zones):
            return True
        clause = enclosing_clause(position)
        return any(marker in clause for marker in preference_markers)

    mandatory: dict[str, int] = {}
    preferred: dict[str, int] = {}
    consumed: list[tuple[int, int]] = []

    for alias in _ALIAS_ORDER:
        if not alias:
            continue
        canonical = _ALIAS_INDEX[alias]
        # Word-boundary match so "js" does not fire inside "json".
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", re.IGNORECASE)
        for match in pattern.finditer(lowered):
            start, end = match.span()
            if any(s <= start < e for s, e in consumed):
                continue  # already covered by a longer alias
            consumed.append((start, end))
            bucket = preferred if in_preferred_zone(start) else mandatory
            bucket.setdefault(canonical, start)
            break  # one hit per alias is enough

    # A skill named as both mandatory and preferred is mandatory.
    for canonical in list(preferred):
        if canonical in mandatory:
            preferred.pop(canonical)

    technologies = sorted(
        {
            SKILL_TO_TECHNOLOGY[SKILL_VOCABULARY[skill][0]]
            for skill in (*mandatory, *preferred)
            if SKILL_VOCABULARY[skill][0] in SKILL_TO_TECHNOLOGY
        }
    )

    mandatory_names = sorted(mandatory, key=lambda name: mandatory[name])
    preferred_names = sorted(preferred, key=lambda name: preferred[name])

    # Confidence scales with how much we found: one lone skill in a long JD
    # usually means the parser missed the skills section.
    found = len(mandatory_names) + len(preferred_names)
    confidence = 0.9 if found >= 4 else 0.75 if found >= 2 else 0.5 if found == 1 else 0.0

    return (
        FieldExtraction(value=mandatory_names, confidence=confidence if mandatory_names else 0.0),
        FieldExtraction(value=preferred_names, confidence=confidence if preferred_names else 0.0),
        FieldExtraction(value=technologies, confidence=confidence if technologies else 0.0),
    )


def extract_experience(text: str) -> tuple[FieldExtraction, FieldExtraction]:
    # "5-8 years", "5 to 8 years"
    ranged = re.search(
        r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", text, re.IGNORECASE
    )
    if ranged:
        low, high = int(ranged.group(1)), int(ranged.group(2))
        if low <= high <= 45:
            return _field(low, 0.9, ranged), _field(high, 0.9, ranged)

    # "minimum 5 years", "at least 5 years", "5+ years"
    minimum = re.search(
        r"(?:minimum(?:\s+of)?|at\s+least|min\.?)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)",
        text,
        re.IGNORECASE,
    )
    if minimum:
        return _field(int(minimum.group(1)), 0.9, minimum), FieldExtraction()

    plus = re.search(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)", text, re.IGNORECASE)
    if plus:
        return _field(int(plus.group(1)), 0.85, plus), FieldExtraction()

    loose = re.search(r"(\d{1,2})\s*(?:years?|yrs?)\s+(?:of\s+)?experience", text, re.IGNORECASE)
    if loose:
        return _field(int(loose.group(1)), 0.7, loose), FieldExtraction()

    return FieldExtraction(), FieldExtraction()


def extract_duration(text: str) -> FieldExtraction:
    match = re.search(
        r"(?:duration|contract\s+(?:length|period|duration)|term)\s*[:\-–]?\s*"
        r"(\d{1,3})\s*(month|months|mth|mths|year|years)",
        text,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(\d{1,3})\s*[- ]?\s*(month|months|year|years)\s*"
            r"(?:contract|assignment|engagement|extendable|renewable)",
            text,
            re.IGNORECASE,
        )
    if not match:
        return FieldExtraction()

    amount = int(match.group(1))
    months = amount * 12 if match.group(2).lower().startswith("year") else amount
    if not 1 <= months <= 120:
        return FieldExtraction()
    return _field(months, 0.88, match)


def extract_positions(text: str) -> FieldExtraction:
    match = re.search(
        r"(?:no\.?\s*of\s*)?(?:positions?|openings?|vacanc(?:y|ies)|headcount|resources?|nos?\.?)\s*"
        r"[:\-–]?\s*(\d{1,2})\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"\b(\d{1,2})\s*(?:positions?|openings?|vacanc(?:y|ies)|resources?|consultants?\s+required)",
            text,
            re.IGNORECASE,
        )
    if not match:
        return FieldExtraction()

    count = int(match.group(1))
    if not 1 <= count <= 50:
        return FieldExtraction()
    return _field(count, 0.85, match)


def extract_location(text: str) -> tuple[FieldExtraction, FieldExtraction]:
    match = _labelled(
        text, ["location", "work location", "base location", "job location", "place of work"]
    )
    if match:
        value = match.group("value").strip(" .;")
        country = None
        for city, code in LOCATION_COUNTRIES.items():
            if city in value.lower():
                country = code
                break
        return (
            _field(value[:160], 0.92, match, "value"),
            FieldExtraction(value=country, confidence=0.85 if country else 0.0),
        )

    lowered = text.lower()
    for city, code in sorted(LOCATION_COUNTRIES.items(), key=lambda item: -len(item[0])):
        index = lowered.find(city)
        if index >= 0:
            return (
                FieldExtraction(
                    value=city.title(),
                    confidence=0.65,
                    evidence=text[index : index + len(city)],
                    evidence_start=index,
                    evidence_end=index + len(city),
                ),
                FieldExtraction(value=code, confidence=0.65),
            )
    return FieldExtraction(), FieldExtraction()


def extract_work_mode(text: str) -> FieldExtraction:
    lowered = text.lower()
    for mode, keywords in (
        ("HYBRID", ["hybrid"]),
        ("REMOTE", ["fully remote", "remote work", "work from home", "wfh", "offshore"]),
        ("ONSITE", ["onsite", "on-site", "on site", "client site", "office based"]),
    ):
        for keyword in keywords:
            index = lowered.find(keyword)
            if index >= 0:
                return FieldExtraction(
                    value=mode,
                    confidence=0.85,
                    evidence=text[index : index + len(keyword)],
                    evidence_start=index,
                    evidence_end=index + len(keyword),
                )
    return FieldExtraction()


def extract_contract_type(text: str) -> FieldExtraction:
    lowered = text.lower()
    for value, keywords in (
        ("CONTRACT_TO_HIRE", ["contract to hire", "contract-to-hire", "c2h", "temp to perm"]),
        (
            "OUTSOURCED_SERVICE",
            ["managed service", "outsourced service", "manpower supply", "body shopping"],
        ),
        ("PERMANENT", ["permanent", "full time employment", "full-time permanent", "perm role"]),
        ("CONTRACT", ["contract", "contractor", "fixed term", "fixed-term", "temporary"]),
    ):
        for keyword in keywords:
            index = lowered.find(keyword)
            if index >= 0:
                return FieldExtraction(
                    value=value,
                    confidence=0.85,
                    evidence=text[index : index + len(keyword)],
                    evidence_start=index,
                    evidence_end=index + len(keyword),
                )
    return FieldExtraction()


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def extract_rate(
    text: str,
) -> tuple[FieldExtraction, FieldExtraction, FieldExtraction, FieldExtraction]:
    """Return (rate_min, rate_max, currency, unit).

    Confidence is capped at 0.8 for every money field: the review policy never
    auto-accepts a rate, because a wrong rate corrupts every downstream
    commercial number.
    """
    currency_pattern = "|".join(
        re.escape(token) for token in sorted(CURRENCY_TOKENS, key=len, reverse=True)
    )
    amount = r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
    unit_pattern = (
        r"per\s+hour|per\s+hr|hourly|/\s*hr|/\s*hour|"
        r"per\s+day|daily|/\s*day|"
        r"per\s+month|monthly|/\s*month|p\.?m\.?|"
        r"per\s+annum|annually|per\s+year|p\.?a\.?"
    )

    pattern = re.compile(
        rf"(?P<cur1>{currency_pattern})?\s*(?P<low>{amount})"
        rf"(?:\s*(?:-|–|to)\s*(?P<cur2>{currency_pattern})?\s*(?P<high>{amount}))?"
        rf"\s*(?P<cur3>{currency_pattern})?\s*(?P<unit>{unit_pattern})",
        re.IGNORECASE,
    )

    match = pattern.search(text)
    if not match:
        return (FieldExtraction(),) * 4  # type: ignore[return-value]

    low = _to_decimal(match.group("low"))
    high = _to_decimal(match.group("high")) if match.group("high") else None
    if low is None or low <= 0:
        return (FieldExtraction(),) * 4  # type: ignore[return-value]
    if high is not None and high < low:
        low, high = high, low

    token = (match.group("cur1") or match.group("cur2") or match.group("cur3") or "").lower()
    currency = CURRENCY_TOKENS.get(token)

    unit_text = re.sub(r"[\s./]", "", match.group("unit").lower())
    if unit_text in {"perhour", "perhr", "hourly", "hr", "hour"}:
        unit = "HOURLY"
    elif unit_text in {"perday", "daily", "day"}:
        unit = "DAILY"
    elif unit_text in {"permonth", "monthly", "month", "pm"}:
        unit = "MONTHLY"
    else:
        unit = "ANNUAL"

    # Money is never auto-accepted, so confidence stays under the 0.85 threshold.
    return (
        _field(str(low), 0.8, match),
        _field(str(high), 0.8, match) if high is not None else FieldExtraction(),
        _field(currency, 0.8 if currency else 0.0, match) if currency else FieldExtraction(),
        _field(unit, 0.8, match, "unit"),
    )


def extract_deadline(text: str, *, now: datetime | None = None) -> FieldExtraction:
    """Submission deadlines are usually relative ("within 24 hours")."""
    reference = now or datetime.now(UTC)

    relative = re.search(
        r"(?:submit|submission|response|revert|profiles?|cvs?)[^.\n]{0,60}?"
        r"within\s+(\d{1,3})\s*(hours?|hrs?|days?|working\s+days?)",
        text,
        re.IGNORECASE,
    )
    if not relative:
        relative = re.search(
            r"within\s+(\d{1,3})\s*(hours?|hrs?|days?|working\s+days?)[^.\n]{0,40}"
            r"(?:submit|submission|response|profiles?|cvs?)",
            text,
            re.IGNORECASE,
        )

    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = (
            timedelta(hours=amount) if unit.startswith(("hour", "hr")) else timedelta(days=amount)
        )
        if timedelta(hours=1) <= delta <= timedelta(days=60):
            return _field((reference + delta).isoformat(), 0.8, relative)

    explicit = _labelled(
        text,
        ["submission deadline", "deadline", "closing date", "last date", "respond by", "submit by"],
    )
    if explicit:
        parsed = _parse_date(explicit.group("value"))
        if parsed:
            return _field(parsed.isoformat(), 0.8, explicit, "value")

    return FieldExtraction()


_DATE_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y-%m-%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B",
    "%d %b",
)


def _parse_date(raw: str, *, now: datetime | None = None) -> datetime | None:
    reference = now or datetime.now(UTC)
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip(" .;"), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)[:40]

    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        if "%Y" not in fmt:
            # A day/month with no year means the next occurrence, not year 1900.
            parsed = parsed.replace(year=reference.year)
            if parsed < reference:
                parsed = parsed.replace(year=reference.year + 1)
        return parsed
    return None


def extract_start_date(text: str, *, now: datetime | None = None) -> FieldExtraction:
    reference = now or datetime.now(UTC)

    match = _labelled(
        text, ["start date", "expected start", "joining date", "commencement", "mobilisation"]
    )
    if match:
        value = match.group("value").strip()
        if re.search(r"immediate|asap|urgent", value, re.IGNORECASE):
            return _field((reference + timedelta(days=14)).date().isoformat(), 0.7, match, "value")
        parsed = _parse_date(value, now=reference)
        if parsed:
            return _field(parsed.date().isoformat(), 0.8, match, "value")

    immediate = re.search(
        r"immediate\s+(?:joiners?|start|availability|requirement)", text, re.IGNORECASE
    )
    if immediate:
        return _field((reference + timedelta(days=14)).date().isoformat(), 0.6, immediate)

    return FieldExtraction()


def extract_availability(text: str) -> FieldExtraction:
    match = _labelled(text, ["notice period", "availability", "joining"])
    if match:
        return _field(match.group("value").strip()[:160], 0.8, match, "value")

    notice = re.search(
        r"(?:maximum|max\.?|within|up\s+to)\s*(\d{1,3})\s*(?:days?|weeks?)\s*notice",
        text,
        re.IGNORECASE,
    )
    if notice:
        return _field(notice.group(0).strip()[:160], 0.75, notice)
    return FieldExtraction()


def extract_customer(text: str) -> FieldExtraction:
    match = _labelled(text, ["client", "customer", "end client", "end customer", "account"])
    if match:
        value = match.group("value").strip(" .;")
        if 1 < len(value) <= 200 and not re.match(
            r"^(confidential|undisclosed|n/?a)$", value, re.IGNORECASE
        ):
            return _field(value, 0.85, match, "value")
    return FieldExtraction()


def extract_project(text: str) -> FieldExtraction:
    match = _labelled(text, ["project", "programme", "program", "engagement"])
    if match:
        value = match.group("value").strip(" .;")
        if 1 < len(value) <= 200:
            return _field(value, 0.8, match, "value")
    return FieldExtraction()


# ------------------------------------------------------------------ provider


class NullLLMProvider:
    """Rule-based extraction. No network, no API key, fully reproducible."""

    name = "null"
    model_id = "deterministic-jd-parser-v1"

    async def extract_requirement(self, text: str) -> ExtractionResult:
        return parse_requirement_text(text)

    async def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
        # Narrative generation has a deterministic template fallback of its own;
        # this provider never invents prose.
        return ""


def parse_requirement_text(text: str, *, now: datetime | None = None) -> ExtractionResult:
    """Extract a structured requirement from raw JD text."""
    warnings: list[str] = []
    if len(text.strip()) < 40:
        warnings.append("The text was very short, so little could be extracted.")

    title = extract_title(text)
    role = extract_role(text, title.value if title.is_present else None)
    mandatory, preferred, technologies = extract_skills(text)
    experience_min, experience_max = extract_experience(text)
    location, country = extract_location(text)
    rate_min, rate_max, currency, rate_unit = extract_rate(text)

    fields: dict[str, FieldExtraction] = {
        "title": title,
        "role": role,
        "mandatory_skills": mandatory,
        "preferred_skills": preferred,
        "technologies": technologies,
        "experience_min_years": experience_min,
        "experience_max_years": experience_max,
        "duration_months": extract_duration(text),
        "positions": extract_positions(text),
        "location": location,
        "country": country,
        "work_mode": extract_work_mode(text),
        "contract_type": extract_contract_type(text),
        "rate_min": rate_min,
        "rate_max": rate_max,
        "rate_currency": currency,
        "rate_unit": rate_unit,
        "response_deadline_at": extract_deadline(text, now=now),
        "start_by_date": extract_start_date(text, now=now),
        "availability_requirement": extract_availability(text),
        "customer_name": extract_customer(text),
        "project_name": extract_project(text),
    }

    present = [field for field in fields.values() if field.is_present]
    overall = round(sum(f.confidence for f in present) / len(present), 3) if present else 0.0

    if not mandatory.is_present:
        warnings.append("No known skills were recognised. Add them manually before matching.")
    if not fields["response_deadline_at"].is_present:
        warnings.append("No submission deadline found. Set one if this came through a VMS.")

    return ExtractionResult(
        fields=fields,
        overall_confidence=overall,
        provider=NullLLMProvider.name,
        model_id=NullLLMProvider.model_id,
        warnings=warnings,
    )


__all__ = [
    "NullLLMProvider",
    "extract_availability",
    "extract_contract_type",
    "extract_customer",
    "extract_deadline",
    "extract_duration",
    "extract_experience",
    "extract_location",
    "extract_positions",
    "extract_project",
    "extract_rate",
    "extract_role",
    "extract_skills",
    "extract_start_date",
    "extract_title",
    "extract_work_mode",
    "parse_requirement_text",
]
