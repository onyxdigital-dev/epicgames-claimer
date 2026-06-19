import asyncio
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def start_scheduler():
    from .claimer import run_claim_job

    tz = os.environ.get("TZ", "America/New_York")

    scheduler.add_job(
        run_claim_job,
        CronTrigger(day_of_week="thu", hour=11, minute=0, timezone=tz),
        id="weekly_claim",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info("Scheduler started — claim job runs every Thursday at 11:00 AM (%s)", tz)


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
