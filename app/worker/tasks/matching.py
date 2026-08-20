"""Scheduled matching work.

The zero-bench sweep is the reason this module exists. It has to run on a clock:
a consultant rolling off in 30 days does not generate a page view, and the whole
point of the engine is to notice before anybody thinks to look.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.db.session import SessionFactory
from app.services.reverse_matching import BenchSweepService
from app.worker.celery_app import celery_app

logger = get_logger("worker.matching")


async def _sweep() -> dict[str, int]:
    # Its own session and its own transaction: a scheduled sweep has no request
    # to inherit one from, and a partial commit here would re-alert tomorrow.
    async with SessionFactory() as session:
        result = await BenchSweepService(session).run()
        await session.commit()
        return result


@celery_app.task(name="app.worker.tasks.matching.sweep_zero_bench")
def sweep_zero_bench() -> dict[str, Any]:
    """Raise redeployment alerts at the 90/60/30/15/7-day milestones.

    Safe to run more than once a day: alerts are deduped per (resource,
    milestone), so a retry after a failure re-raises nothing already sent.
    """
    result = asyncio.run(_sweep())
    logger.info("zero_bench_sweep_finished", **result)
    return dict(result)


__all__ = ["sweep_zero_bench"]
