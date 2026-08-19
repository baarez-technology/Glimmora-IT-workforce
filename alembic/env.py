"""Alembic environment.

The database URL comes from application settings rather than alembic.ini, so
there is exactly one place that decides which database is in use.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings
from app.db.base import Base

# Importing the model registry populates Base.metadata for autogenerate.
import app.models  # noqa: F401  # isort: skip

config = context.config

# Application settings decide the database, unless a caller supplied a URL
# explicitly (the migration test points at a throwaway file, and operators
# can target a specific database without changing the environment).
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(_object, name, type_, _reflected, _compare_to) -> bool:
    # Alembic's own bookkeeping table must never appear in a migration.
    return not (type_ == "table" and name == "alembic_version")


def _render_item(type_, obj, autogen_context) -> str | bool:
    """Render custom column types in a form a migration can actually execute.

    Two of the portable types in `app.db.types` do not survive autogenerate:
    `StrEnumType` carries a Python enum class that cannot be expressed (and is a
    plain VARCHAR in the database anyway, since the coercion is application-side),
    and the JSON/JSONB variant renders a nested `Text()` that is never imported.
    Both are rendered here into something a migration can actually execute.
    """
    import sqlalchemy as sa

    from app.db.types import StrEnumType

    if type_ != "type":
        return False

    if isinstance(obj, StrEnumType):
        autogen_context.imports.add("import sqlalchemy as sa")
        return f"sa.String(length={obj.length})"

    # The JSON/JSONB variant renders with an un-imported `Text()` inside it.
    # Referencing the shared alias keeps one definition of the type.
    if isinstance(obj, sa.JSON):
        autogen_context.imports.add("import app.db.types")
        return "app.db.types.JSONType"

    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_item=_render_item,
        # Batch mode lets SQLite handle ALTER TABLE, keeping the
        # no-infrastructure path migratable rather than recreate-only.
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_item=_render_item,
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
