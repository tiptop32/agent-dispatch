from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime

import httpx
import uvicorn

from agent_dispatch.api import create_app
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache, build_adapters
from agent_dispatch.routing.decision import build_routers
from agent_dispatch.serve_state import (
    ServeState,
    clear_state,
    is_running,
    port_in_use,
    read_state,
    write_state,
)
from agent_dispatch.telemetry.storage import Storage


async def serve(settings) -> None:
    storage = Storage(settings.server.data_dir / "dispatch.db")
    adapters = build_adapters(settings)
    availability = AvailabilityCache(adapters, settings.routing.availability_ttl_seconds)
    async with httpx.AsyncClient() as client:
        dispatcher = Dispatcher(
            settings, storage, adapters, availability, build_routers(settings, client)
        )
        token = secrets.token_urlsafe(32)
        write_state(
            settings.server.data_dir,
            ServeState(
                pid=os.getpid(),
                port=settings.server.port,
                host=settings.server.host,
                token=token,
                started_at=datetime.now(UTC),
            ),
        )
        app = create_app(settings, dispatcher, storage, availability, token)
        try:
            await uvicorn.Server(
                uvicorn.Config(
                    app, host=settings.server.host, port=settings.server.port, log_level="info"
                )
            ).serve()
        finally:
            clear_state(settings.server.data_dir)


def run_server(settings) -> None:
    state = read_state(settings.server.data_dir)
    # Живого pid мало: он остаётся и у зомби, и у застрявшего в shutdown
    # процесса, а такой демон уже ничего не обслуживает. Порт решает спор.
    if state is not None and is_running(state):
        raise SystemExit(f"daemon already running (pid {state.pid})")
    if not port_is_free(settings.server.host, settings.server.port):
        raise SystemExit(f"port {settings.server.port} is busy")
    asyncio.run(serve(settings))


def port_is_free(host: str, port: int) -> bool:
    """Пробный bind как у uvicorn: семейство адреса по host, SO_REUSEADDR."""
    return not port_in_use(host, port)
