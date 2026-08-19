"""Shared test fixtures.

The suite runs on SQLite with every provider set to ``null``, so it needs no
Docker daemon and no API keys (DEPLOYMENT.md section 6).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

TEST_DB_PATH = Path(__file__).resolve().parent / "_test.db"

# Environment must be set before app.core.config is imported anywhere.
os.environ.update(
    APP_ENV="test",
    DATABASE_URL=f"sqlite+aiosqlite:///{TEST_DB_PATH.as_posix()}",
    VECTOR_BACKEND="memory",
    STORAGE_BACKEND="local",
    CACHE_BACKEND="memory",
    CELERY_TASK_ALWAYS_EAGER="true",
    LLM_PROVIDER="null",
    EMBEDDING_PROVIDER="null",
    EMAIL_TRANSPORT="log",
    RATE_LIMIT_ENABLED="false",
    JWT_SECRET="test-secret-key-not-used-outside-the-test-suite",
    SECRET_KEY="test-secret-key-not-used-outside-the-test-suite",
    LOG_LEVEL="WARNING",
    # Its own storage directory. Without this the suite purges the real
    # development document store, because both resolve to backend/var/documents.
    LOCAL_STORAGE_PATH=str(Path(__file__).resolve().parent / "_documents"),
)

import uuid  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.permissions import Role  # noqa: E402
from app.core.rate_limit import reset_limiter  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionFactory, engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.identity import User  # noqa: E402

#: Meets the password policy, so tests exercise the real validator.
TEST_PASSWORD = "Glimmora-Test-2026!"


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Create the schema once for the session, drop the file afterwards."""
    import asyncio

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        # The technology and skill masters are reference data, not user data.
        # Seeding them here mirrors production, where `python -m app.seed`
        # populates them before anyone signs in. Same event loop as the schema
        # creation: the async engine binds its pool to the loop that first
        # used it.
        from app.seed.accounts import seed_technologies
        from app.seed.demand import seed_skills
        from app.seed.scoring import seed_scoring_configurations

        async with SessionFactory() as db:
            await seed_technologies(db)
            await seed_skills(db)
            # The v1 scoring rule set is reference data too: the engine would
            # seed it lazily, but then version numbers would differ between a
            # seeded deployment and the suite.
            await seed_scoring_configurations(db)
            await db.commit()

    asyncio.run(setup())
    yield
    asyncio.run(engine.dispose())
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(TEST_DB_PATH) + suffix)
        if candidate.exists():
            candidate.unlink()


@pytest.fixture(autouse=True)
def _clean_document_storage():
    """Uploads land on the local filesystem; keep runs isolated."""
    from app.storage.service import purge_local_storage

    purge_local_storage()
    yield
    purge_local_storage()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    reset_limiter()
    yield
    reset_limiter()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session that rolls back, so tests never leak state into each other."""
    async with SessionFactory() as db:
        yield db
        await db.rollback()


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ------------------------------------------------------------------ identity


@pytest.fixture
async def make_user() -> Callable[..., object]:
    """Create a user directly in the database.

    Emails are unique per call so cases stay independent despite the shared
    session-scoped schema.
    """

    async def _make(
        role: Role = Role.ADMIN,
        *,
        password: str = TEST_PASSWORD,
        is_active: bool = True,
        must_change_password: bool = False,
        email: str | None = None,
        full_name: str | None = None,
    ) -> User:
        async with SessionFactory() as db:
            user = User(
                email=email or f"{role.value.lower()}-{uuid.uuid4().hex[:10]}@test.glimmora.ai",
                full_name=full_name or f"Test {role.value.title()}",
                hashed_password=hash_password(password),
                role=role,
                is_active=is_active,
                must_change_password=must_change_password,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return _make


@pytest.fixture
async def sign_in(client: AsyncClient) -> Callable[..., object]:
    """Log a user in through the real endpoint and return (token, payload)."""

    async def _sign_in(user: User, password: str = TEST_PASSWORD) -> tuple[str, dict]:
        response = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": password}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        return body["access_token"], body

    return _sign_in


@pytest.fixture
async def as_role(app, make_user, client: AsyncClient) -> Callable[..., object]:
    """An authenticated client for the given role.

    Returns the same client instance with an Authorization header applied, so
    cookies set during login stay attached.
    """

    async def _as_role(role: Role) -> tuple[AsyncClient, User]:
        user = await make_user(role)
        response = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        return client, user

    return _as_role
