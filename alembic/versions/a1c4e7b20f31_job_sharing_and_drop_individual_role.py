"""job sharing between colleagues, and the removal of the INDIVIDUAL role

Two changes that belong together: the job feed stops being an external
person's private tool and becomes a Sales/Resourcing workspace, so it gains
handover columns and loses the role that used to be its only audience.

Revision ID: a1c4e7b20f31
Revises: c90f7a7bc001
Create Date: 2026-10-01 19:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401  # custom column types used by autogenerate

revision: str = "a1c4e7b20f31"
down_revision: str | None = "3f05caaa1d12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- handover columns on the feed ------------------------------------
    # batch_alter_table so this works on SQLite too, which cannot ALTER a
    # column or add a named constraint in place.
    with op.batch_alter_table("job_feed_items") as batch:
        batch.add_column(sa.Column("shared_by_user_id", app.db.types.GUID(), nullable=True))
        batch.add_column(sa.Column("share_note", sa.Text(), nullable=True))
        batch.add_column(sa.Column("shared_at", app.db.types.UTCDateTime(), nullable=True))
        batch.add_column(
            sa.Column(
                "share_acknowledged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.create_foreign_key(
            "fk_job_feed_items_shared_by",
            "users",
            ["shared_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index(
        "ix_job_feed_items_shared_by_user_id", "job_feed_items", ["shared_by_user_id"]
    )
    op.create_index("ix_job_feed_items_shared_at", "job_feed_items", ["shared_at"])
    op.create_index(
        "ix_job_feed_items_share_acknowledged", "job_feed_items", ["share_acknowledged"]
    )

    # --- retire the INDIVIDUAL role --------------------------------------
    # Role is stored as a plain string, so there is no enum type to alter.
    # Any account still holding it is deactivated rather than deleted or
    # silently promoted: these were self-registered external people, and
    # quietly turning one into staff would be the worst of the three options.
    # Their feed rows are left intact, so reactivating under a real role
    # loses nothing.
    op.execute(
        """
        UPDATE users
           SET is_active = 0,
               role = 'HR_RESOURCING'
         WHERE role = 'INDIVIDUAL'
        """
        if op.get_bind().dialect.name == "sqlite"
        else """
        UPDATE users
           SET is_active = false,
               role = 'HR_RESOURCING'
         WHERE role = 'INDIVIDUAL'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_job_feed_items_share_acknowledged", table_name="job_feed_items")
    op.drop_index("ix_job_feed_items_shared_at", table_name="job_feed_items")
    op.drop_index("ix_job_feed_items_shared_by_user_id", table_name="job_feed_items")

    with op.batch_alter_table("job_feed_items") as batch:
        batch.drop_constraint("fk_job_feed_items_shared_by", type_="foreignkey")
        batch.drop_column("share_acknowledged")
        batch.drop_column("shared_at")
        batch.drop_column("share_note")
        batch.drop_column("shared_by_user_id")

    # The INDIVIDUAL accounts are not restored: which deactivated users were
    # once individuals is not recoverable from the schema, and inventing that
    # mapping would be worse than leaving them deactivated.
