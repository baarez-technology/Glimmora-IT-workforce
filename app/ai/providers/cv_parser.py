"""Deterministic CV parser — the offline counterpart to the JD parser.

One rule differs from JD parsing and matters a great deal: **experience years
are computed from the extracted date ranges, never taken from the CV's own
summary line.** Candidates routinely round "8+ years" up, and that number feeds
the Experience component of every match score.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from app.ai.base import ExtractionResult, FieldExtraction
from app.ai.vocabulary import (
    COUNTRY_TOKENS,
    LOCATION_COUNTRIES,
    ROLE_KEYWORDS,
    build_alias_index,
)

_ALIAS_INDEX = build_alias_index()
_ALIAS_ORDER = sorted(_ALIAS_INDEX, key=len, reverse=True)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+\d{1,3}[\s\-.]?)?(?:\(\d{1,4}\)[\s\-.]?)?\d[\d\s\-.]{7,14}\d")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_PATTERN = "|".join(_MONTHS)

#: "Jan 2019 - Mar 2022", "01/2019 to present", "2019 – 2022"
_DATE_RANGE = re.compile(
    rf"(?P<from>(?:(?:{_MONTH_PATTERN})[a-z]*\.?\s+)?(?:\d{{1,2}}[/-])?\d{{4}})"
    r"\s*(?:-|–|—|to|until|till)\s*"
    rf"(?P<to>present|current|now|ongoing|(?:(?:{_MONTH_PATTERN})[a-z]*\.?\s+)?(?:\d{{1,2}}[/-])?\d{{4}})",
    re.IGNORECASE,
)

_NOTICE = re.compile(
    r"notice\s*(?:period)?\s*[:\-–]?\s*(?P<value>immediate|immediately|"
    r"(?P<amount>\d{1,3})\s*(?P<unit>days?|weeks?|months?))",
    re.IGNORECASE,
)

_SECTION_HEADINGS = (
    "professional summary",
    "profile",
    "summary",
    "objective",
    "about me",
)


def _field(value: object, confidence: float, evidence: str | None = None) -> FieldExtraction:
    return FieldExtraction(value=value, confidence=confidence, evidence=evidence)


def _parse_month_year(raw: str, *, default_month: int) -> date | None:
    text = raw.strip().lower().rstrip(".")
    if text in {"present", "current", "now", "ongoing"}:
        return datetime.now(UTC).date()

    month_match = re.match(rf"({_MONTH_PATTERN})[a-z]*\.?\s+(\d{{4}})", text)
    if month_match:
        return date(int(month_match.group(2)), _MONTHS[month_match.group(1)], 1)

    numeric = re.match(r"(\d{1,2})[/-](\d{4})", text)
    if numeric:
        month = int(numeric.group(1))
        if 1 <= month <= 12:
            return date(int(numeric.group(2)), month, 1)

    year_only = re.fullmatch(r"(\d{4})", text)
    if year_only:
        year = int(year_only.group(1))
        if 1970 <= year <= datetime.now(UTC).year + 1:
            return date(year, default_month, 1)
    return None


def extract_name(text: str) -> FieldExtraction:
    """The first line that looks like a person's name.

    Deliberately conservative: a wrong name on a candidate record is worse than
    an empty one, so anything containing an email, a digit or a job-title word is
    rejected.
    """
    for line in text.splitlines()[:8]:
        candidate = line.strip(" #*-•\t|")
        if not (3 < len(candidate) <= 60):
            continue
        if _EMAIL.search(candidate) or any(char.isdigit() for char in candidate):
            continue
        lowered = candidate.lower()
        if any(word in lowered for word in ("curriculum", "resume", "cv", "profile", "@")):
            continue

        words = candidate.split()
        if 2 <= len(words) <= 4 and all(word[:1].isupper() for word in words if word):
            return _field(candidate, 0.75, candidate)
    return FieldExtraction()


def extract_contact(text: str) -> tuple[FieldExtraction, FieldExtraction]:
    email_match = _EMAIL.search(text)
    email = (
        _field(email_match.group(0).lower(), 0.95, email_match.group(0))
        if email_match
        else FieldExtraction()
    )

    phone = FieldExtraction()
    for match in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        # A 4-digit year or a reference number is not a phone number.
        if 9 <= len(digits) <= 15:
            phone = _field(match.group(0).strip(), 0.8, match.group(0))
            break
    return email, phone


def extract_location(text: str) -> tuple[FieldExtraction, FieldExtraction]:
    """Return (city, country).

    A CV that says "Doha, Qatar" must yield Doha as the city. Longest-match
    alone would pick "Qatar" for both, so city tokens are searched first and
    country tokens only supply the country.
    """
    lowered = text.lower()

    def first_hit(tokens: list[str]) -> tuple[str, str, int] | None:
        best: tuple[str, str, int] | None = None
        for token in tokens:
            index = lowered.find(token)
            if index >= 0 and (best is None or index < best[2]):
                best = (token, LOCATION_COUNTRIES[token], index)
        return best

    cities = [token for token in LOCATION_COUNTRIES if token not in COUNTRY_TOKENS]
    countries = [token for token in LOCATION_COUNTRIES if token in COUNTRY_TOKENS]

    city_hit = first_hit(cities)
    country_hit = first_hit(countries)

    city_field = _field(city_hit[0].title(), 0.7, city_hit[0]) if city_hit else FieldExtraction()
    # The country is more reliable than the city, so it can come from either.
    source = country_hit or city_hit
    country_field = _field(source[1], 0.75, source[0]) if source else FieldExtraction()

    return city_field, country_field


def extract_headline(text: str) -> FieldExtraction:
    """The role the candidate leads with, from a title line or a summary."""
    lowered = text.lower()
    for role, keywords in ROLE_KEYWORDS:
        for keyword in keywords:
            index = lowered.find(keyword)
            # Only trust it near the top; a keyword deep in a CV is job history.
            if 0 <= index < 600:
                return _field(role, 0.8, keyword)
    return FieldExtraction()


def extract_summary(text: str) -> FieldExtraction:
    for heading in _SECTION_HEADINGS:
        match = re.search(rf"(?:^|\n)\s*{re.escape(heading)}\s*[:\n]", text, re.IGNORECASE)
        if not match:
            continue
        body = text[match.end() : match.end() + 900].strip()
        # Stop at the next section heading.
        stop = re.search(r"\n\s*[A-Z][A-Za-z /&]{3,40}\s*[:\n]", body)
        if stop:
            body = body[: stop.start()]
        body = body.strip()
        if len(body) > 40:
            return _field(body[:1200], 0.8, heading)
    return FieldExtraction()


def extract_experience_entries(text: str) -> tuple[FieldExtraction, FieldExtraction]:
    """Return (entries, total_years).

    Total years is computed from the union of the extracted date ranges — union,
    not sum, so overlapping roles are not double-counted.
    """
    entries: list[dict[str, object]] = []
    intervals: list[tuple[date, date]] = []
    today = datetime.now(UTC).date()

    for match in _DATE_RANGE.finditer(text):
        start = _parse_month_year(match.group("from"), default_month=1)
        end = _parse_month_year(match.group("to"), default_month=12)
        if start is None or end is None or end < start or start.year < 1970:
            continue
        end = min(end, today)
        if end < start:
            continue

        # The line above a date range is almost always the role or employer.
        line_start = text.rfind("\n", 0, match.start()) + 1
        preceding = text[max(0, line_start - 200) : match.start()].strip().splitlines()
        context = preceding[-1].strip(" -–|•\t") if preceding else ""

        entries.append(
            {
                "role": context[:160] or None,
                "start_date": start.isoformat(),
                "end_date": None
                if match.group("to").lower() in {"present", "current", "now", "ongoing"}
                else end.isoformat(),
                "is_current": match.group("to").lower() in {"present", "current", "now", "ongoing"},
            }
        )
        intervals.append((start, end))

    if not intervals:
        return FieldExtraction(), FieldExtraction()

    # Merge overlapping intervals before totalling.
    intervals.sort()
    merged: list[list[date]] = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    months = sum((end.year - start.year) * 12 + (end.month - start.month) for start, end in merged)
    years = round(months / 12, 1)

    return (
        _field(entries, 0.8 if len(entries) >= 2 else 0.6),
        # Computed, never read from the CV's own claim.
        _field(years, 0.85 if len(merged) >= 1 and years > 0 else 0.0),
    )


def extract_skills(text: str) -> tuple[FieldExtraction, FieldExtraction]:
    """Return (skills, technologies) found anywhere in the CV."""
    lowered = text.lower()
    found: dict[str, int] = {}
    consumed: list[tuple[int, int]] = []

    for alias in _ALIAS_ORDER:
        if not alias:
            continue
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", re.IGNORECASE)
        for match in pattern.finditer(lowered):
            start, end = match.span()
            if any(s <= start < e for s, e in consumed):
                continue
            consumed.append((start, end))
            found.setdefault(_ALIAS_INDEX[alias], start)
            break

    from app.ai.vocabulary import SKILL_TO_TECHNOLOGY, SKILL_VOCABULARY

    technologies = sorted(
        {
            SKILL_TO_TECHNOLOGY[SKILL_VOCABULARY[skill][0]]
            for skill in found
            if SKILL_VOCABULARY[skill][0] in SKILL_TO_TECHNOLOGY
        }
    )
    names = sorted(found, key=lambda name: found[name])
    confidence = 0.9 if len(names) >= 5 else 0.75 if len(names) >= 2 else 0.5 if names else 0.0

    return _field(names, confidence), _field(technologies, confidence if technologies else 0.0)


def extract_notice_period(text: str) -> FieldExtraction:
    match = _NOTICE.search(text)
    if not match:
        return FieldExtraction()

    if match.group("value").lower().startswith("immediat"):
        return _field(0, 0.85, match.group(0))

    amount = int(match.group("amount"))
    unit = match.group("unit").lower()
    days = (
        amount * 30
        if unit.startswith("month")
        else amount * 7
        if unit.startswith("week")
        else amount
    )
    return _field(days, 0.85, match.group(0)) if 0 <= days <= 365 else FieldExtraction()


def extract_certifications(text: str) -> FieldExtraction:
    known = (
        "PMP",
        "PRINCE2",
        "ITIL",
        "CISSP",
        "CISM",
        "CEH",
        "AWS Certified",
        "Azure Administrator",
        "Azure Solutions Architect",
        "CCNA",
        "CCNP",
        "TOGAF",
        "Scrum Master",
        "SAFe",
        "ISO 27001 Lead Auditor",
    )
    found = [name for name in known if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE)]
    return _field(found, 0.8 if found else 0.0)


def parse_cv_text(text: str) -> ExtractionResult:
    """Extract a structured candidate profile from CV text."""
    warnings: list[str] = []
    if len(text.strip()) < 120:
        warnings.append("The document contained very little text, so little could be extracted.")

    email, phone = extract_contact(text)
    city, country = extract_location(text)
    entries, total_years = extract_experience_entries(text)
    skills, technologies = extract_skills(text)

    fields: dict[str, FieldExtraction] = {
        "full_name": extract_name(text),
        "email": email,
        "phone": phone,
        "headline": extract_headline(text),
        "summary": extract_summary(text),
        "current_location_city": city,
        "current_location_country": country,
        "total_experience_years": total_years,
        "experience_entries": entries,
        "skills": skills,
        "technologies": technologies,
        "certifications": extract_certifications(text),
        "notice_period_days": extract_notice_period(text),
    }

    present = [field for field in fields.values() if field.is_present]
    overall = round(sum(f.confidence for f in present) / len(present), 3) if present else 0.0

    if not skills.is_present:
        warnings.append("No known skills were recognised. Add them manually before matching.")
    if not email.is_present and not phone.is_present:
        warnings.append("No contact details found — check the document is not a scanned image.")
    if not total_years.is_present:
        warnings.append(
            "Could not compute experience from dates. Enter the total manually rather than "
            "trusting a claim in the CV text."
        )

    return ExtractionResult(
        fields=fields,
        overall_confidence=overall,
        provider="null",
        model_id="deterministic-cv-parser-v1",
        warnings=warnings,
    )


__all__ = [
    "extract_certifications",
    "extract_contact",
    "extract_experience_entries",
    "extract_headline",
    "extract_location",
    "extract_name",
    "extract_notice_period",
    "extract_skills",
    "extract_summary",
    "parse_cv_text",
]
