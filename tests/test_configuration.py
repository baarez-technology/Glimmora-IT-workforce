"""Configuration drift guards.

A setting that exists in code but not in `.env.example` is invisible to whoever
deploys this, and a documented variable the application ignores is a lie. Both
are cheap to catch and expensive to discover in production.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

#: Supplied by Docker Compose or the frontend, not by the Settings model.
NON_SETTINGS_KEYS = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "NEXT_PUBLIC_API_BASE_URL",
}

#: Internal knobs with safe defaults that operators are not expected to tune.
UNDOCUMENTED_BY_DESIGN = {
    "APP_NAME",
    "API_V1_PREFIX",
    "REQUEST_MAX_BYTES",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "VECTOR_DIMENSIONS",
    "LOCAL_STORAGE_PATH",
    "MAX_UPLOAD_BYTES",
    "SIGNED_URL_TTL_SECONDS",
    "JWT_ALGORITHM",
    "PASSWORD_MIN_LENGTH",
    "LOGIN_MAX_ATTEMPTS",
    "LOGIN_LOCKOUT_MINUTES",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "EMBEDDING_BATCH_SIZE",
    "AI_DAILY_TOKEN_CEILING",
    "AI_CIRCUIT_BREAKER_FAILURES",
    "AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
    "RATE_LIMIT_ENABLED",
    "RATE_LIMIT_LOGIN_PER_MINUTE",
    "RATE_LIMIT_PARSING_PER_MINUTE",
    "RATE_LIMIT_DEFAULT_PER_MINUTE",
}


def _documented_keys() -> set[str]:
    return {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }


def _settings_fields() -> set[str]:
    from app.core.config import Settings

    return set(Settings.model_fields)


class TestEnvExample:
    def test_every_operator_facing_setting_is_documented(self):
        missing = _settings_fields() - _documented_keys() - UNDOCUMENTED_BY_DESIGN
        assert missing == set(), f"Add these to .env.example: {sorted(missing)}"

    def test_no_documented_variable_is_ignored_by_the_application(self):
        unknown = _documented_keys() - _settings_fields() - NON_SETTINGS_KEYS
        assert unknown == set(), f".env.example documents unused keys: {sorted(unknown)}"

    def test_the_template_carries_no_real_secrets(self):
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        for value in re.findall(r"^[A-Z_]+=(.+)$", content, flags=re.MULTILINE):
            assert "sk-" not in value, "an API key leaked into the template"
            assert not value.startswith("eyJ"), "a JWT leaked into the template"

    def test_ai_defaults_to_offline_and_keeps_rates_out_of_prompts(self):
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "LLM_PROVIDER=null" in content
        assert "AI_ALLOW_COMMERCIAL_CONTEXT=false" in content


class TestCompose:
    @pytest.mark.parametrize(
        "service", ["postgres", "redis", "qdrant", "minio", "api", "worker", "beat", "web"]
    )
    def test_service_is_defined(self, service):
        content = COMPOSE_FILE.read_text(encoding="utf-8")
        assert f"\n  {service}:" in content

    def test_workers_are_not_hidden_behind_a_profile(self):
        """SLA, document-expiry and zero-bench alerts are core V1, not optional."""
        content = COMPOSE_FILE.read_text(encoding="utf-8")
        assert "profiles:" not in content

    def test_a_missing_env_file_does_not_break_a_fresh_clone(self):
        content = COMPOSE_FILE.read_text(encoding="utf-8")
        assert "required: false" in content
        assert "\n    env_file: .env\n" not in content

    def test_the_web_container_stays_same_origin(self):
        """Forcing a cross-origin API URL would break the httpOnly refresh cookie."""
        content = COMPOSE_FILE.read_text(encoding="utf-8")
        assert "API_ORIGIN: http://api:8000" in content

        # Comments may mention the variable; what matters is that no service
        # actually sets it.
        directives = [
            line.strip() for line in content.splitlines() if not line.strip().startswith("#")
        ]
        assert not any(line.startswith("NEXT_PUBLIC_API_BASE_URL") for line in directives)

    def test_the_documents_bucket_is_created_private(self):
        content = COMPOSE_FILE.read_text(encoding="utf-8")
        assert "mc anonymous set none" in content
