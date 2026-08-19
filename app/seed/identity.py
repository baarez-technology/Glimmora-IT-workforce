"""Seed the four platform roles with demo users.

Idempotent: re-running updates nothing and creates nothing twice. Seeding is a
CLI command rather than a migration so production never receives demo rows by
accident (DATABASE.md section 5).
"""

from __future__ import annotations

import os
import secrets
import string

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.permissions import Role
from app.core.security import hash_password
from app.models.identity import User
from app.repositories.identity import UserRepository

logger = get_logger("seed")

SEED_USERS: list[dict[str, object]] = [
    {
        "email": "admin@glimmora.ai",
        "full_name": "Platform Administrator",
        "job_title": "IT Administrator",
        "role": Role.ADMIN,
    },
    {
        "email": "management@glimmora.ai",
        "full_name": "Aisha Al-Kuwari",
        "job_title": "Head of Delivery",
        "role": Role.MANAGEMENT,
    },
    {
        "email": "sales@glimmora.ai",
        "full_name": "Daniel Fernandes",
        "job_title": "Account Manager",
        "role": Role.SALES,
    },
    {
        "email": "resourcing@glimmora.ai",
        "full_name": "Priya Raghavan",
        "job_title": "Resourcing Lead",
        "role": Role.HR_RESOURCING,
    },
]


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Glm-" + "".join(secrets.choice(alphabet) for _ in range(14))


async def seed_users(session: AsyncSession) -> list[tuple[str, str | None]]:
    """Create any missing seed users. Returns (email, password) per user created."""
    repo = UserRepository(session)
    created: list[tuple[str, str | None]] = []

    # A shared password is acceptable for a local demo only. Anywhere else, each
    # account gets its own generated password printed once and never stored.
    shared_password = os.getenv("SEED_PASSWORD")

    for spec in SEED_USERS:
        email = str(spec["email"])
        if await repo.get_by_email(email):
            logger.info("seed_user_exists", email=email)
            created.append((email, None))
            continue

        password = shared_password or _generate_password()
        user = User(
            email=email,
            full_name=str(spec["full_name"]),
            job_title=str(spec["job_title"]),
            role=spec["role"],  # type: ignore[arg-type]
            hashed_password=hash_password(password),
            is_active=True,
            # Generated credentials must not survive first contact.
            must_change_password=shared_password is None,
        )
        await repo.add(user)
        created.append((email, password))
        logger.info("seed_user_created", email=email, role=str(spec["role"]))

    return created


__all__ = ["SEED_USERS", "seed_users"]
