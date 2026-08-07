"""APScheduler job registration."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import AppConfig
from app.service import PumpService

logger = logging.getLogger(__name__)


def create_scheduler(config: AppConfig, service: PumpService) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        service.poll_forecasts,
        "interval",
        minutes=config.weather.poll_minutes,
        id="poll_forecasts",
        replace_existing=True,
    )
    scheduler.add_job(
        service.run_evaluation,
        "interval",
        minutes=config.rules.evaluate_minutes,
        id="run_evaluation",
        replace_existing=True,
    )

    return scheduler
