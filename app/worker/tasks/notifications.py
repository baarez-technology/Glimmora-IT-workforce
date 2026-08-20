"""Scheduled notification sweeps.

Every sweep is idempotent by dedupe key, so a retry after a failure re-raises
nothing already sent. That is what makes it safe to run these on a schedule and
to re-run them by hand when something looks wrong.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.db.session import SessionFactory
from app.services.notifications import NotificationSweeps
from app.worker.celery_app import celery_app

logger = get_logger("worker.notifications")


async def _run(sweep: str) -> dict[str, int]:
    # Its own session and transaction: a scheduled sweep has no request to
    # inherit one from, and a partial commit would re-alert tomorrow.
    async with SessionFactory() as session:
        sweeps = NotificationSweeps(session)
        result = await getattr(sweeps, sweep)()
        await session.commit()
        return result


@celery_app.task(name="app.worker.tasks.notifications.sweep_submission_sla")
def sweep_submission_sla() -> dict[str, Any]:
    """Hourly. A VMS window can be 24 hours; daily would miss half of them."""
    result = asyncio.run(_run("sweep_submission_sla"))
    logger.info("sla_sweep_finished", **result)
    return dict(result)


@celery_app.task(name="app.worker.tasks.notifications.sweep_document_expiry")
def sweep_document_expiry() -> dict[str, Any]:
    """Daily. An expired work permit stops billing on a live deployment."""
    result = asyncio.run(_run("sweep_document_expiry"))
    logger.info("document_expiry_sweep_finished", **result)
    return dict(result)


@celery_app.task(name="app.worker.tasks.notifications.sweep_follow_up_overdue")
def sweep_follow_up_overdue() -> dict[str, Any]:
    """Daily. A pipeline stops moving one missed follow-up at a time."""
    result = asyncio.run(_run("sweep_follow_up_overdue"))
    logger.info("follow_up_sweep_finished", **result)
    return dict(result)


@celery_app.task(name="app.worker.tasks.notifications.sweep_project_ending")
def sweep_project_ending() -> dict[str, Any]:
    """Daily. A project ending is a renewal conversation, not an surprise."""
    result = asyncio.run(_run("sweep_project_ending"))
    logger.info("project_ending_sweep_finished", **result)
    return dict(result)


__all__ = [
    "sweep_document_expiry",
    "sweep_follow_up_overdue",
    "sweep_project_ending",
    "sweep_submission_sla",
]
