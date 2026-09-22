from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse


def install_auth(app, settings, token: str) -> None:
    port = settings.server.port
    allowed = {"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"}

    @app.middleware("http")
    async def auth_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ):
        host = request.headers.get("host", "")
        if host not in allowed:
            return JSONResponse({"detail": "misdirected host"}, status_code=421)
        if request.url.path != "/health":
            value = request.headers.get("authorization", "")
            expected = f"Bearer {token}"
            if not secrets.compare_digest(value, expected):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)
