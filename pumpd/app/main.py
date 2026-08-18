"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import EnvSettings, load_config
from app.db import init_db
from app.scheduler import create_scheduler
from app.service import PumpService
from app.web.routes import create_router


def setup_logging(json_logs: bool) -> None:
    if json_logs:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    service: PumpService = app.state.service
    scheduler = app.state.scheduler
    await service.startup()
    scheduler.start()
    service.scheduler_running = True
    await service.run_evaluation()
    yield
    scheduler.shutdown(wait=False)
    await service.shutdown()


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    setup_logging(config.logging.json_logs)

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    env = EnvSettings()
    session_factory = init_db(config.database_url)
    service = PumpService(
        config,
        session_factory,
        smartthings_pat=env.smartthings_pat,
        tuya_api_key=env.tuya_api_key,
        tuya_api_secret=env.tuya_api_secret,
        tuya_api_region=env.tuya_api_region,
        tuya_api_device_id=env.tuya_api_device_id,
        meross_email=env.meross_email,
        meross_password=env.meross_password,
        meross_api_base=env.meross_api_base,
        meross_mfa_code=env.meross_mfa_code,
        meross_lan_first=config.devices.meross_lan_first,
        google_oauth_client_id=env.google_oauth_client_id,
        google_oauth_client_secret=env.google_oauth_client_secret,
        config_path=config_path,
    )
    scheduler = create_scheduler(config, service)

    app = FastAPI(title="pumpd", version="1.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.service = service
    app.state.scheduler = scheduler
    app.include_router(create_router(config, service, scheduler))

    static_dir = Path(__file__).resolve().parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


app = create_app()
