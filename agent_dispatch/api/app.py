from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from .auth import install_auth
from .routes import build_router


@dataclass
class AppDeps:
    settings: object
    dispatcher: object
    storage: object
    availability: object
    token: str
    started_at: float


def create_app(settings, dispatcher, storage, availability, token: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await storage.open()
        recovered = await dispatcher.recover_stale()
        logging.getLogger("agent_dispatch.api").info("recovered %d stale tasks", recovered)
        await availability.check_all()
        try:
            yield
        finally:
            await dispatcher.shutdown()
            await storage.close()

    app = FastAPI(lifespan=lifespan)
    app.state.deps = AppDeps(settings, dispatcher, storage, availability, token, time.monotonic())
    install_auth(app, settings, token)
    app.include_router(build_router())
    return app
