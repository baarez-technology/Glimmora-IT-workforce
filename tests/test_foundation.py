"""Phase 2 foundation tests: health, config, error contract, security headers."""

from __future__ import annotations

import pytest

API = "/api/v1"


class TestHealth:
    async def test_health_reports_status_and_components(self, client):
        response = await client.get(f"{API}/system/health")
        assert response.status_code == 200

        body = response.json()
        assert body["status"] in {"healthy", "degraded"}
        assert body["environment"] == "test"

        names = {component["name"] for component in body["components"]}
        assert names == {
            "database",
            "vector_store",
            "object_storage",
            "cache",
            "queue",
            "llm",
            "embeddings",
            "email",
        }

    async def test_fallbacks_are_reported_not_hidden(self, client):
        """A degraded run must never look like a healthy one."""
        body = (await client.get(f"{API}/system/health")).json()

        assert body["status"] == "degraded"
        assert "llm" in body["degraded"]
        assert "vector_store" in body["degraded"]

        database = next(c for c in body["components"] if c["name"] == "database")
        assert database["state"] in {"ok", "fallback"}

    async def test_public_config_contains_no_secrets(self, client):
        body = (await client.get(f"{API}/system/config")).json()

        assert body["base_currency"] == "QAR"
        assert body["ai_enabled"] is False
        assert body["bench_milestone_days"] == [90, 60, 30, 15, 7]

        serialized = str(body).lower()
        for forbidden in ("secret", "password", "api_key", "token"):
            assert forbidden not in serialized


class TestErrorContract:
    async def test_not_found_uses_the_error_envelope(self, client):
        response = await client.get(f"{API}/does-not-exist")
        assert response.status_code == 404

        error = response.json()["error"]
        assert error["code"] == "NOT_FOUND"
        assert error["message"] == "That page or record could not be found."
        assert error["request_id"]

    async def test_validation_error_lists_offending_fields(self, client, app):
        """A 422 must name the offending fields, not just say "invalid"."""
        from fastapi import Depends

        from app.core.pagination import PageParams, page_params

        @app.get("/api/v1/_test/paged")
        async def _paged(params: PageParams = Depends(page_params)) -> dict[str, int]:
            return {"page": params.page}

        response = await client.get(f"{API}/_test/paged", params={"page": 0, "page_size": 5000})
        assert response.status_code == 422

        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        fields = {detail["field"] for detail in error["details"]}
        assert fields == {"page", "page_size"}
        assert error["request_id"]

    async def test_unknown_sort_field_is_rejected_not_ignored(self):
        """Silently dropping a bad sort makes list screens look non-deterministic."""
        from app.core.errors import ValidationError
        from app.core.pagination import PageParams, apply_sort

        params = PageParams(sort="-not_a_column")
        with pytest.raises(ValidationError) as excinfo:
            apply_sort(None, None, params, allowed={"created_at", "name"})  # type: ignore[arg-type]
        assert "created_at" in excinfo.value.details[0]["message"]

    def test_pagination_envelope_reports_page_count(self):
        from app.core.pagination import Page, PageParams

        params = PageParams(page=2, page_size=25)
        page = Page.build(items=[1, 2, 3], total=128, params=params)
        assert (page.total, page.page, page.page_size, page.pages) == (128, 2, 25, 6)
        assert Page.empty(params).pages == 0

    async def test_request_id_is_echoed_when_supplied(self, client):
        response = await client.get(
            f"{API}/system/health", headers={"X-Request-ID": "trace-me-123"}
        )
        assert response.headers["X-Request-ID"] == "trace-me-123"

    async def test_request_id_is_generated_when_absent(self, client):
        response = await client.get(f"{API}/system/health")
        assert len(response.headers["X-Request-ID"]) == 32


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ],
    )
    async def test_headers_present(self, client, header, expected):
        response = await client.get(f"{API}/system/health")
        assert response.headers[header] == expected

    async def test_headers_present_on_error_responses_too(self, client):
        response = await client.get(f"{API}/nope")
        assert response.status_code == 404
        assert response.headers["X-Content-Type-Options"] == "nosniff"


class TestLoggingRedaction:
    def test_sensitive_keys_are_redacted(self):
        from app.core.logging import redaction_processor

        event = redaction_processor(
            None,
            "info",
            {
                "event": "login",
                "password": "hunter2",
                "email": "someone@example.com",
                "nested": {"refresh_token": "abc123", "safe": "value"},
            },
        )

        assert event["password"] == "[redacted]"
        assert event["email"] == "[redacted]"
        assert event["nested"]["refresh_token"] == "[redacted]"
        assert event["nested"]["safe"] == "value"

    def test_emails_inside_free_text_are_redacted(self):
        from app.core.logging import redaction_processor

        event = redaction_processor(None, "info", {"event": "x", "note": "wrote to a@b.com today"})
        assert "a@b.com" not in event["note"]


class TestPortableTypes:
    def test_money_never_built_from_float_precision(self):
        from decimal import Decimal

        from app.db.types import money

        assert money(1234.567) == Decimal("1234.57")
        assert money("0.1") + money("0.2") == Decimal("0.30")
        assert money(None) is None

    def test_utc_datetime_normalises_naive_input(self):
        from datetime import UTC, datetime

        from app.db.types import UTCDateTime

        column = UTCDateTime()
        naive = datetime(2026, 8, 18, 9, 0)
        assert column.process_bind_param(naive, None).tzinfo == UTC

    def test_utcnow_is_timezone_aware(self):
        from app.db.types import utcnow

        assert utcnow().tzinfo is not None


class TestSettings:
    def test_degraded_components_named_explicitly(self):
        from app.core.config import settings

        degraded = settings.degraded_components()
        assert degraded["llm"].startswith("deterministic")
        assert "queue" in degraded

    def test_bench_milestones_match_the_sow(self):
        from app.core.config import settings

        assert settings.bench_milestone_days == [90, 60, 30, 15, 7]

    def test_document_reminder_days_descend(self):
        from app.core.config import settings

        assert settings.document_reminder_days == [90, 60, 30, 7]


class TestBackgroundJobs:
    def test_eager_mode_runs_tasks_inline_without_a_worker(self):
        """Callers never branch on whether Celery is available."""
        from app.worker.celery_app import celery_app, ping

        assert celery_app.conf.task_always_eager is True
        assert ping.delay().get() == "pong"

    def test_beat_schedule_is_declared(self):
        from app.worker.celery_app import celery_app

        # Sweeps are enabled by the phases that own them; the schedule object
        # must exist from Phase 2 so beat can start.
        assert isinstance(celery_app.conf.beat_schedule, dict)
