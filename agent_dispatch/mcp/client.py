from __future__ import annotations

import httpx

from agent_dispatch.models import DispatchRequest, RouteDecision, TaskView
from agent_dispatch.serve_state import ServeState


class DaemonUnavailable(Exception):
    """The local daemon could not be reached or authenticated."""


class DispatchClient:
    def __init__(self, state: ServeState, timeout: float = 30.0):
        self.state = state
        self.base_url = f"http://{state.host}:{state.port}"
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {state.token}"},
            timeout=timeout,
        )

    async def __aenter__(self) -> DispatchClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise DaemonUnavailable(f"daemon at {self.base_url} is not reachable: {exc}") from exc
        if response.status_code == 401:
            raise DaemonUnavailable("token rejected, restart the daemon")
        if response.is_error:
            raise RuntimeError(f"{response.status_code}: {response.text}")
        return response

    async def health(self) -> dict:
        return (await self._request("GET", "/health")).json()

    async def route(self, req: DispatchRequest) -> RouteDecision:
        response = await self._request("POST", "/route", json=req.model_dump(mode="json"))
        return RouteDecision.model_validate(response.json())

    async def submit(self, req: DispatchRequest) -> TaskView:
        response = await self._request("POST", "/tasks", json=req.model_dump(mode="json"))
        return TaskView.model_validate(response.json())

    async def status(self, task_id: str) -> TaskView:
        try:
            response = await self._request("GET", f"/tasks/{task_id}")
        except RuntimeError as exc:
            if "404" in str(exc):
                raise ValueError(f"task not found: {task_id}") from exc
            raise
        return TaskView.model_validate(response.json())

    async def cancel(self, task_id: str) -> TaskView:
        response = await self._request("DELETE", f"/tasks/{task_id}")
        return TaskView.model_validate(response.json())

    async def executors(self) -> list[dict]:
        return (await self._request("GET", "/executors")).json()

    async def feedback(self, task_id: str, outcome: str, note: str | None = None) -> dict:
        return (
            await self._request(
                "POST", f"/tasks/{task_id}/feedback", json={"outcome": outcome, "note": note}
            )
        ).json()

    async def export(self, since: str) -> str:
        return (await self._request("GET", "/export", params={"since": since})).text
