"""Seed runner: ``python -m app.seed``.

Each phase registers its seeders here. Every seeder must be idempotent so the
command can be re-run against a partially seeded database.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import SessionFactory, dispose_engine
from app.seed.accounts import seed_accounts, seed_technologies
from app.seed.demand import seed_requirements, seed_skills
from app.seed.identity import seed_users
from app.seed.scoring import seed_scoring_configurations
from app.seed.talent import seed_resources

logger = get_logger("seed")


async def run() -> int:
    configure_logging()

    if settings.is_production:
        print("Refusing to seed demo data in production.", file=sys.stderr)
        return 2

    async with SessionFactory() as session:
        created = await seed_users(session)
        await session.flush()
        technologies = await seed_technologies(session)
        await session.flush()
        accounts = await seed_accounts(session)
        await session.flush()
        skills = await seed_skills(session)
        await session.flush()
        demand = await seed_requirements(session)
        await session.flush()
        talent = await seed_resources(session)
        await session.flush()
        scoring = await seed_scoring_configurations(session)
        # Phase 8+: opportunities, deployments, billing.
        await session.commit()

    new_accounts = [(email, password) for email, password in created if password]

    print()
    print("Glimmora seed complete.")
    print(f"  users present: {len(created)}   created now: {len(new_accounts)}")
    print(f"  technologies: +{technologies}")
    print(
        f"  accounts: +{accounts['accounts']}"
        f"   routes: +{accounts['routes']}"
        f"   contacts: +{accounts['contacts']}"
        f"   projects: +{accounts['projects']}"
        f"   activities: +{accounts['activities']}"
    )
    print(
        f"  skills: +{skills}"
        f"   requirements: +{demand['requirements']}"
        f"   requirement skills: +{demand['skills_linked']}"
    )
    print(
        f"  resources: +{talent['resources']}"
        f"   resource skills: +{talent['skills']}"
        f"   documents: +{talent['documents']}"
    )
    print(f"  scoring configurations: +{scoring}")

    if new_accounts:
        print()
        print("  Sign-in credentials (shown once — they are not stored anywhere):")
        for email, password in new_accounts:
            print(f"    {email:<28} {password}")
        print()
        print("  Each account must change its password at first sign-in unless")
        print("  SEED_PASSWORD was set.")
    else:
        print("  No new accounts — existing users were left untouched.")
    print()

    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
