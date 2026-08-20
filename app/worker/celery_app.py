"""Celery application and beat schedule.

Background work exists for three reasons in this platform: AI parsing is slow,
matching is expensive, and the alert engines (SLA, document expiry, zero-bench)
must run on a clock rather than on a page view.

When Redis is unavailable, ``CELERY_TASK_ALWAYS_EAGER`` makes every task run
inline in the calling process. Callers therefore never branch on whether a
worker exists (ARCHITECTURE.md section 6).
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("worker")

celery_app = Celery(
    "glimmora",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_eager_propagates=settings.CELERY_TASK_ALWAYS_EAGER,
    task_track_started=True,
    task_time_limit=600,
    task_soft_time_limit=540,
    worker_max_tasks_per_child=200,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
)

# Task modules are registered here as each phase delivers them.
TASK_MODULES: tuple[str, ...] = (
    # "app.worker.tasks.parsing",        # Phase 5 / 6
    # "app.worker.tasks.embeddings",     # Phase 6
    "app.worker.tasks.matching",  # Phase 8 — zero-bench sweep
    "app.worker.tasks.notifications",  # Phase 12 — SLA, expiry, follow-ups
)
celery_app.conf.imports = TASK_MODULES

# Scheduled sweeps. Times are UTC; Qatar is UTC+3, so 03:00 UTC is 06:00 local —
# alerts land before the working day rather than during it.
celery_app.conf.beat_schedule = {
    # Phase 12 — submission SLA deadlines (VMS windows are 24-48 hours, so this
    # runs hourly rather than daily).
    "sla-sweep": {
        "task": "app.worker.tasks.notifications.sweep_submission_sla",
        "schedule": crontab(minute=5),
    },
    # Phase 6 / 12 — visa and work-permit expiry.
    "document-expiry-sweep": {
        "task": "app.worker.tasks.notifications.sweep_document_expiry",
        "schedule": crontab(hour=3, minute=0),
    },
    # Phase 12 — opportunities whose next action has slipped.
    "follow-up-sweep": {
        "task": "app.worker.tasks.notifications.sweep_follow_up_overdue",
        "schedule": crontab(hour=4, minute=0),
    },
    # Phase 12 — client projects approaching their end.
    "project-ending-sweep": {
        "task": "app.worker.tasks.notifications.sweep_project_ending",
        "schedule": crontab(hour=4, minute=30),
    },
    # Phase 8 — zero-bench: 90/60/30/15/7 days before a deployment ends.
    "zero-bench-sweep": {
        "task": "app.worker.tasks.matching.sweep_zero_bench",
        "schedule": crontab(hour=3, minute=30),
    },
}


@celery_app.on_after_configure.connect
def _configure_worker_logging(**_kwargs: object) -> None:
    configure_logging()
    logger.info(
        "worker_configured",
        eager=settings.CELERY_TASK_ALWAYS_EAGER,
        broker="memory" if settings.CELERY_TASK_ALWAYS_EAGER else "redis",
    )


@celery_app.task(name="app.worker.ping")
def ping() -> str:
    """Liveness task used by the health check and the Phase 2 smoke test."""
    return "pong"


__all__ = ["celery_app", "crontab", "ping"]
