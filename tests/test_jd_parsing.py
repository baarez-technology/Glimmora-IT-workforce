"""Phase 5: the deterministic JD parser and the AI resilience contract.

These tests matter more than usual: `LLM_PROVIDER=null` is the default, so this
parser is what an offline Glimmora deployment actually runs on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ai.base import ExtractionResult, FieldExtraction
from app.ai.extraction.text import extract_text, normalise_text, validate_document
from app.ai.providers.null_provider import parse_requirement_text
from app.core.errors import DocumentParseError, UnsupportedMediaTypeError
from app.models.demand import RateUnit, WorkMode

FIXED_NOW = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)

FULL_JD = """Job Title: Senior SAP FICO Consultant
Client: Milaha
Project: S/4HANA Finance Rollout
Location: Doha, Qatar
Contract Type: Contract (extendable)
Duration: 18 months contract
No. of Positions: 2

We are looking for a Senior SAP FICO Consultant with minimum 8 years of experience
in SAP S/4HANA implementations. The candidate must be hands-on with SAP FICO,
SAP MM integration and ABAP debugging. Experience with Oracle EBS migration is
nice to have. Knowledge of Power BI is desirable.

Work Mode: Hybrid
Rate: QAR 18,000 - 22,000 per month
Notice Period: maximum 30 days notice
Start Date: 15/09/2026

Please submit profiles within 48 hours of receiving this requirement.
"""


class TestFullExtraction:
    def test_a_realistic_jd_yields_a_usable_requirement(self):
        result = parse_requirement_text(FULL_JD, now=FIXED_NOW)

        assert result.value("title") == "Senior SAP FICO Consultant"
        assert result.value("role") == "SAP Consultant"
        assert result.value("location") == "Doha, Qatar"
        assert result.value("country") == "QA"
        assert result.value("work_mode") == "HYBRID"
        assert result.value("contract_type") == "CONTRACT"
        assert result.value("experience_min_years") == 8
        assert result.value("duration_months") == 18
        assert result.value("positions") == 2
        assert result.value("customer_name") == "Milaha"
        assert result.value("project_name") == "S/4HANA Finance Rollout"

    def test_skills_split_into_mandatory_and_preferred(self):
        result = parse_requirement_text(FULL_JD, now=FIXED_NOW)

        mandatory = result.value("mandatory_skills")
        preferred = result.value("preferred_skills")

        assert "SAP FICO" in mandatory
        assert "SAP S/4HANA" in mandatory
        assert "SAP MM" in mandatory
        # Both phrasings of "preferred" must land in the preferred bucket.
        assert "Oracle EBS" in preferred, "an inline 'nice to have' was misread as mandatory"
        assert "Power BI" in preferred

    def test_technologies_are_derived_from_the_skills(self):
        result = parse_requirement_text(FULL_JD, now=FIXED_NOW)
        assert set(result.value("technologies")) >= {"SAP", "Oracle", "Microsoft"}

    def test_every_extracted_field_carries_evidence_from_the_source(self):
        """A reviewer who cannot see where a value came from merely trusts it."""
        result = parse_requirement_text(FULL_JD, now=FIXED_NOW)

        for name in ("title", "location", "duration_months", "rate_min"):
            field = result.get(name)
            assert field.evidence, f"{name} has no evidence"
            assert field.evidence_start is not None
            assert field.evidence.strip() in FULL_JD


class TestConfidencePolicy:
    @pytest.mark.parametrize(
        "field", ["rate_min", "rate_max", "rate_currency", "rate_unit", "response_deadline_at"]
    )
    def test_money_and_dates_never_reach_auto_accept_confidence(self, field):
        """A wrong rate corrupts every downstream commercial number."""
        result = parse_requirement_text(FULL_JD, now=FIXED_NOW)
        assert result.get(field).confidence < 0.85

    def test_a_labelled_value_scores_higher_than_an_inferred_one(self):
        labelled = parse_requirement_text("Location: Doha, Qatar\n" + "x" * 100)
        inferred = parse_requirement_text("The role is based in our Doha office. " + "x" * 100)

        assert labelled.get("location").confidence > inferred.get("location").confidence

    def test_a_field_that_is_absent_is_left_absent_not_guessed(self):
        result = parse_requirement_text(
            "Job Title: Java Developer\nWe need a strong backend engineer for our team in Dubai."
        )

        assert not result.get("rate_min").is_present
        assert not result.get("duration_months").is_present
        assert not result.get("response_deadline_at").is_present

    def test_skill_confidence_reflects_how_much_was_found(self):
        many = parse_requirement_text("Java, Spring Boot, Kubernetes, AWS and Terraform required.")
        one = parse_requirement_text(
            "We need somebody who knows Java. " + "Other duties apply. " * 20
        )

        assert many.get("mandatory_skills").confidence > one.get("mandatory_skills").confidence


class TestRateExtraction:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Rate: QAR 18,000 - 22,000 per month", ("18000", "22000", "QAR", "MONTHLY")),
            ("Budget is USD 65 per hour", ("65", None, "USD", "HOURLY")),
            ("Daily rate of AED 1,200 per day", ("1200", None, "AED", "DAILY")),
            ("Salary: 240000 per annum", ("240000", None, None, "ANNUAL")),
        ],
    )
    def test_rate_shapes(self, text, expected):
        result = parse_requirement_text(text + "\n" + "filler " * 20)
        low, high, currency, unit = expected

        assert result.value("rate_min") == low
        assert result.value("rate_max") == high
        assert result.value("rate_currency") == currency
        assert result.value("rate_unit") == unit

    def test_a_reversed_range_is_corrected(self):
        result = parse_requirement_text("Rate: QAR 22,000 - 18,000 per month\n" + "filler " * 20)
        assert result.value("rate_min") == "18000"
        assert result.value("rate_max") == "22000"


class TestDeadlineExtraction:
    def test_a_relative_window_becomes_an_absolute_deadline(self):
        result = parse_requirement_text(
            "Please submit CVs within 24 hours.\n" + "filler " * 20, now=FIXED_NOW
        )
        deadline = datetime.fromisoformat(result.value("response_deadline_at"))
        assert deadline == FIXED_NOW + timedelta(hours=24)

    def test_a_day_window_works_too(self):
        result = parse_requirement_text(
            "Submission within 3 working days please.\n" + "filler " * 20, now=FIXED_NOW
        )
        deadline = datetime.fromisoformat(result.value("response_deadline_at"))
        assert deadline == FIXED_NOW + timedelta(days=3)

    def test_an_unrelated_within_phrase_is_not_treated_as_a_deadline(self):
        result = parse_requirement_text(
            "The system must respond within 2 seconds under load.\n" + "filler " * 20,
            now=FIXED_NOW,
        )
        assert not result.get("response_deadline_at").is_present

    def test_a_missing_deadline_is_warned_about(self):
        result = parse_requirement_text("Job Title: Java Developer\n" + "filler " * 30)
        assert any("deadline" in warning.lower() for warning in result.warnings)


class TestExperienceExtraction:
    @pytest.mark.parametrize(
        ("text", "low", "high"),
        [
            ("Minimum 8 years of experience required", 8, None),
            ("5-8 years experience in SAP", 5, 8),
            ("At least 6 years experience", 6, None),
            ("10+ years experience", 10, None),
            ("5 to 7 years experience", 5, 7),
        ],
    )
    def test_experience_shapes(self, text, low, high):
        result = parse_requirement_text(text + "\n" + "filler " * 20)
        assert result.value("experience_min_years") == low
        assert result.value("experience_max_years") == high


class TestParserRobustness:
    def test_empty_text_produces_no_crash_and_an_honest_warning(self):
        result = parse_requirement_text("   ")
        assert result.overall_confidence == 0.0
        assert result.warnings

    def test_prose_with_no_recognisable_structure_extracts_little(self):
        result = parse_requirement_text(
            "We are a growing organisation seeking talented individuals to join our team."
        )
        assert not result.get("rate_min").is_present
        assert any("skills" in warning.lower() for warning in result.warnings)

    def test_a_short_alias_does_not_fire_inside_a_longer_word(self):
        """'js' must not match inside 'json', or every JD gains a phantom skill."""
        result = parse_requirement_text(
            "The service returns json payloads and uses jsonb columns. " + "filler " * 20
        )
        assert "JavaScript" not in (result.value("mandatory_skills") or [])

    def test_the_longest_alias_wins(self):
        result = parse_requirement_text("Strong SAP FICO background required. " + "filler " * 20)
        skills = result.value("mandatory_skills") or []
        assert "SAP FICO" in skills

    def test_parsing_is_reproducible(self):
        first = parse_requirement_text(FULL_JD, now=FIXED_NOW)
        second = parse_requirement_text(FULL_JD, now=FIXED_NOW)
        assert first.model_dump() == second.model_dump()


class TestDocumentValidation:
    def test_a_supported_text_file_is_accepted(self):
        assert validate_document("jd.txt", b"x" * 200, max_bytes=1024 * 1024) == "txt"

    def test_an_unsupported_extension_is_refused(self):
        with pytest.raises(UnsupportedMediaTypeError):
            validate_document("payload.exe", b"MZ", max_bytes=1024 * 1024)

    def test_a_mislabelled_file_is_refused_on_its_magic_bytes(self):
        """The extension alone is never trusted (SECURITY.md section 6)."""
        with pytest.raises(DocumentParseError):
            validate_document("jd.pdf", b"this is not a pdf at all", max_bytes=1024 * 1024)

    def test_an_oversized_file_is_refused(self):
        with pytest.raises(DocumentParseError):
            validate_document("jd.txt", b"x" * 5000, max_bytes=1000)

    def test_a_document_with_too_little_text_gives_a_usable_message(self):
        with pytest.raises(DocumentParseError) as excinfo:
            extract_text("jd.txt", b"tiny", max_bytes=1024 * 1024)
        assert "manually" in str(excinfo.value).lower()

    def test_extraction_normalises_pdf_whitespace_noise(self):
        messy = "Title\r\n\r\n\r\n   Lots    of   space \t\n• bullet"
        cleaned = normalise_text(messy)
        assert "\n\n\n" not in cleaned
        assert "   " not in cleaned
        assert "- bullet" in cleaned


class TestAIResilience:
    async def test_a_provider_failure_falls_back_and_says_so(self, monkeypatch):
        """AI failure degrades data richness; it never blocks the workflow."""
        from app.ai import registry

        class BrokenProvider:
            name = "anthropic"
            model_id = "broken"

            async def extract_requirement(self, text: str):
                raise RuntimeError("provider exploded")

            async def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
                return ""

        registry.reset_provider()
        monkeypatch.setattr(registry, "_provider", BrokenProvider())

        result = await registry.extract_requirement(FULL_JD)

        assert result.used_fallback is True
        assert result.value("title") == "Senior SAP FICO Consultant"
        assert any("did not succeed" in warning for warning in result.warnings)
        registry.reset_provider()

    async def test_the_circuit_opens_after_repeated_failures(self, monkeypatch):
        from app.ai import registry

        calls = {"count": 0}

        class BrokenProvider:
            name = "anthropic"
            model_id = "broken"

            async def extract_requirement(self, text: str):
                calls["count"] += 1
                raise RuntimeError("still broken")

            async def complete(self, prompt: str, *, max_tokens: int = 1024) -> str:
                return ""

        registry.reset_provider()
        monkeypatch.setattr(registry, "_provider", BrokenProvider())
        monkeypatch.setattr(registry, "_breaker", registry.CircuitBreaker(1, 300))

        await registry.extract_requirement("first attempt " * 20)
        attempts_after_first = calls["count"]

        await registry.extract_requirement("second attempt " * 20)

        # The breaker is open, so the second call must not reach the provider.
        assert calls["count"] == attempts_after_first
        registry.reset_provider()

    def test_the_null_provider_is_selected_when_no_key_is_configured(self):
        from app.ai.providers.null_provider import NullLLMProvider
        from app.ai.registry import get_llm_provider, reset_provider

        reset_provider()
        assert isinstance(get_llm_provider(), NullLLMProvider)


class TestExtractionResultContract:
    def test_absent_and_empty_values_are_both_treated_as_missing(self):
        assert not FieldExtraction(value=None).is_present
        assert not FieldExtraction(value=[]).is_present
        assert not FieldExtraction(value="").is_present
        assert FieldExtraction(value=0).is_present
        assert FieldExtraction(value=["Java"]).is_present

    def test_confident_and_uncertain_fields_are_separable(self):
        result = ExtractionResult(
            fields={
                "title": FieldExtraction(value="X", confidence=0.9),
                "role": FieldExtraction(value="Y", confidence=0.4),
                "location": FieldExtraction(value=None, confidence=0.0),
            }
        )
        assert result.confident_fields(0.85) == ["title"]
        assert result.uncertain_fields(0.85) == ["role"]


class TestExtractionHardening:
    """A real model can return values the database will not accept.

    The deterministic parser never does, so this path is only exercised on the
    LLM route — where an unvalidated value used to reach flush and 500, turning
    a slightly-wrong extraction into a lost job description.
    """

    @staticmethod
    def _apply(fields: dict[str, object]):
        from app.models.demand import Requirement
        from app.services.requirements import RequirementService

        result = ExtractionResult(
            fields={
                name: FieldExtraction(value=value, confidence=0.9) for name, value in fields.items()
            }
        )
        requirement = Requirement(title="draft")
        RequirementService._apply_extraction(None, requirement, result)  # type: ignore[arg-type]
        return requirement

    def test_an_invalid_enum_is_dropped_not_written(self):
        requirement = self._apply({"work_mode": "FLEXIBLE", "contract_type": "FULL_TIME"})
        assert requirement.work_mode is None
        assert requirement.contract_type is None

    def test_a_valid_enum_still_applies_case_insensitively(self):
        requirement = self._apply({"work_mode": "hybrid", "rate_unit": "monthly"})
        assert requirement.work_mode is WorkMode.HYBRID
        assert requirement.rate_unit is RateUnit.MONTHLY

    def test_a_country_name_is_rejected_but_an_iso_code_is_kept(self):
        assert self._apply({"country": "Qatar"}).country is None
        assert self._apply({"country": "qa"}).country == "QA"

    def test_a_currency_name_is_rejected_but_an_iso_code_is_kept(self):
        assert self._apply({"rate_currency": "Qatari Riyal"}).rate_currency is None
        assert self._apply({"rate_currency": "qar"}).rate_currency == "QAR"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("positions", 999),
            ("positions", 0),
            ("experience_min_years", -5),
            ("duration_months", 9999),
            ("duration_months", 0),
        ],
    )
    def test_out_of_range_numbers_are_dropped(self, field, value):
        assert getattr(self._apply({field: value}), field) is None

    def test_in_range_numbers_are_kept(self):
        requirement = self._apply({"positions": 3, "duration_months": 18})
        assert requirement.positions == 3
        assert requirement.duration_months == 18

    def test_a_reversed_rate_range_is_corrected_rather_than_stored_backwards(self):
        requirement = self._apply({"rate_min": "22000", "rate_max": "18000"})
        assert str(requirement.rate_min) == "18000"
        assert str(requirement.rate_max) == "22000"

    def test_an_inverted_experience_range_drops_the_weaker_half(self):
        requirement = self._apply({"experience_min_years": 8, "experience_max_years": 5})
        assert requirement.experience_min_years == 8
        assert requirement.experience_max_years is None

    def test_over_long_text_is_truncated_to_the_column(self):
        requirement = self._apply({"title": "x" * 500, "role": "y" * 500})
        assert len(requirement.title) == 240
        assert len(requirement.role) == 160

    def test_a_non_string_where_text_is_expected_is_ignored(self):
        requirement = self._apply({"title": 42, "location": {"city": "Doha"}})
        assert requirement.title == "draft"
        assert requirement.location is None
