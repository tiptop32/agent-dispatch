from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime

import httpx
import pytest

import agent_dispatch.api.routes as routes
from agent_dispatch.api import create_app
from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    RouteDecision,
    RouterKind,
    TaskStatus,
)
from agent_dispatch.serve_state import ServeState, write_state
from agent_dispatch.server import run_server
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter


@pytest.fixture
async def api(tmp_path, git_repo):
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path, port=7433),
        routing=RoutingSettings(fallback_executor="codex"),
        executors={
            "codex": ExecutorSettings(adapter="codex", description="Codex"),
            "claude": ExecutorSettings(adapter="claude", description="Claude"),
        },
    )
    storage = Storage(tmp_path / "db.sqlite")
    adapters = {n: FakeAdapter(n) for n in settings.executors}
    availability = AvailabilityCache(adapters, 60)
    dispatcher = Dispatcher(
        settings,
        storage,
        adapters,
        availability,
        [
            FakeRouter(
                RouteDecision(
                    executor="codex", confidence=1, scores={"codex": 1}, router=RouterKind.fallback
                )
            )
        ],
    )
    await storage.open()
    await availability.check_all()
    app = create_app(settings, dispatcher, storage, availability, "secret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:7433") as client:
        yield client, dispatcher, storage, adapters, git_repo, settings
    await dispatcher.shutdown()
    await storage.close()


def headers(token="secret", host="127.0.0.1:7433"):
    return {"host": host, "authorization": f"Bearer {token}"}


def payload(repo, **extra):
    return {"task": "fix tests", "cwd": str(repo), "executor": "codex", **extra}


@pytest.mark.asyncio
async def test_health_without_token(api):
    r = await api[0].get("/health")
    assert r.status_code == 200 and "version" in r.json() and "router_backend" in r.json()


@pytest.mark.asyncio
async def test_auth_required(api):
    assert (await api[0].get("/executors")).status_code == 401


@pytest.mark.asyncio
async def test_bad_token_rejected(api):
    assert (await api[0].get("/executors", headers=headers("bad"))).status_code == 401


@pytest.mark.asyncio
async def test_evil_host_rejected(api):
    assert (await api[0].get("/health", headers={"host": "evil.example:7433"})).status_code == 421


@pytest.mark.asyncio
async def test_localhost_accepted(api):
    assert (await api[0].get("/health", headers={"host": "localhost:7433"})).status_code == 200


@pytest.mark.asyncio
async def test_executors(api):
    data = (await api[0].get("/executors", headers=headers())).json()
    assert {x["name"] for x in data} == {"codex", "claude"}
    fields = {"name", "adapter", "model", "enabled", "available", "version", "checked_at", "error"}
    assert all(set(item) == fields and item["available"] is True for item in data)


@pytest.mark.asyncio
async def test_route_and_export_null_task(api):
    client, _, storage, _, repo, _ = api
    response = await client.post("/route", headers=headers(), json=payload(repo))
    assert response.status_code == 200
    assert response.json()["executor"] == "codex"
    RouteDecision.model_validate(response.json())
    rows = [x async for x in storage.export()]
    assert any(x["task"] is None for x in rows)


@pytest.mark.asyncio
async def test_invalid_task_body(api):
    assert (await api[0].post("/tasks", headers=headers(), json={"cwd": "/tmp"})).status_code == 422


@pytest.mark.asyncio
async def test_task_wait_zero_then_get_with_log(api):
    client, dispatcher, _, adapters, repo, _ = api
    adapters["codex"].result = ExecutionResult(
        status="completed", executor="codex", model=None, summary="ok"
    )
    r = await client.post("/tasks", headers=headers(), json=payload(repo, wait_seconds=0))
    assert r.status_code == 202
    task_id = r.json()["task_id"]
    record = await dispatcher.wait(task_id, 2)
    from pathlib import Path

    await asyncio.to_thread(Path(record.log_path).write_text, "log tail")
    got = await client.get(f"/tasks/{task_id}", headers=headers())
    assert (
        got.status_code == 200
        and got.json()["status"] == "completed"
        and got.json()["log_tail"] == "log tail"
    )


@pytest.mark.asyncio
async def test_task_wait_completed(api):
    r = await api[0].post("/tasks", headers=headers(), json=payload(api[4], wait_seconds=5))
    assert r.status_code == 200 and r.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_task_wait_is_capped(api, monkeypatch):
    client, _, _, adapters, repo, _ = api
    adapters["codex"].gate = asyncio.Event()
    monkeypatch.setattr(routes, "MAX_WAIT_SECONDS", 0.2)
    started = asyncio.get_running_loop().time()
    response = await client.post(
        "/tasks", headers=headers(), json=payload(repo, wait_seconds=99999)
    )
    elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 202
    assert elapsed < 1


@pytest.mark.asyncio
async def test_unknown_task_get(api):
    assert (await api[0].get("/tasks/unknown", headers=headers())).status_code == 404


@pytest.mark.asyncio
async def test_get_task_wait_returns_final_status(api):
    client, _, _, adapters, repo, _ = api
    adapters["codex"].gate = asyncio.Event()
    response = await client.post("/tasks", headers=headers(), json=payload(repo, wait_seconds=0))
    task_id = response.json()["task_id"]
    await adapters["codex"].started.wait()

    async def finish():
        await asyncio.sleep(0.01)
        adapters["codex"].gate.set()

    finisher = asyncio.create_task(finish())
    response = await client.get(f"/tasks/{task_id}?wait=1", headers=headers())
    await finisher
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_get_task_wait_timeout_returns_running(api, monkeypatch):
    client, _, _, adapters, repo, _ = api
    adapters["codex"].gate = asyncio.Event()
    monkeypatch.setattr(routes, "MAX_WAIT_SECONDS", 0.05)
    response = await client.post("/tasks", headers=headers(), json=payload(repo, wait_seconds=0))
    task_id = response.json()["task_id"]
    await adapters["codex"].started.wait()

    started = asyncio.get_running_loop().time()
    response = await client.get(f"/tasks/{task_id}?wait=1", headers=headers())
    elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert 0.04 <= elapsed < 1


@pytest.mark.asyncio
async def test_cancel_running(api):
    client, _, _, adapters, repo, _ = api
    adapters["codex"].gate = asyncio.Event()
    r = await client.post("/tasks", headers=headers(), json=payload(repo, wait_seconds=0))
    task_id = r.json()["task_id"]
    await adapters["codex"].started.wait()
    cancelled = await client.delete(f"/tasks/{task_id}", headers=headers())
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_finished(api):
    client, _, _, _, repo, _ = api
    r = await client.post("/tasks", headers=headers(), json=payload(repo, wait_seconds=5))
    assert (
        await client.delete(f"/tasks/{r.json()['task_id']}", headers=headers())
    ).status_code == 409


@pytest.mark.asyncio
async def test_cancel_unknown(api):
    assert (await api[0].delete("/tasks/nope", headers=headers())).status_code == 404


@pytest.mark.asyncio
async def test_feedback_and_export(api):
    client, _, storage, _, repo, _ = api
    r = await client.post("/tasks", headers=headers(), json=payload(repo, wait_seconds=0))
    task_id = r.json()["task_id"]
    out = await client.post(
        f"/tasks/{task_id}/feedback", headers=headers(), json={"outcome": "success", "note": "ok"}
    )
    assert out.status_code == 200 and out.json()["ok"]
    assert any(e["kind"] == "feedback" for e in await storage.list_events(task_id))
    exported = await client.get("/export", headers=headers())
    rows = [json.loads(line) for line in exported.text.splitlines()]
    row = next(item for item in rows if item["task"] and item["task"]["task_id"] == task_id)
    assert any(event["kind"] == "feedback" for event in row["events"])


@pytest.mark.asyncio
async def test_feedback_unknown(api):
    assert (
        await api[0].post("/tasks/nope/feedback", headers=headers(), json={"outcome": "failure"})
    ).status_code == 404


@pytest.mark.asyncio
async def test_export_since_jsonl(api):
    client, _, _, _, repo, _ = api
    await client.post("/tasks", headers=headers(), json=payload(repo, wait_seconds=5))
    r = await client.get("/export?since=1d", headers=headers())
    assert r.status_code == 200
    assert all(json.loads(line) for line in r.text.splitlines())
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_export_since_invalid(api):
    assert (await api[0].get("/export?since=wat", headers=headers())).status_code == 422


def _state_settings(tmp_path, port: int):
    write_state(
        tmp_path,
        ServeState(
            pid=__import__("os").getpid(), port=port, token="x", started_at=datetime.now(UTC)
        ),
    )
    return Settings(
        server=ServerSettings(data_dir=tmp_path, port=port),
        executors={"codex": ExecutorSettings(adapter="codex")},
        routing=RoutingSettings(fallback_executor="codex"),
    )


def test_run_server_detects_live_state(tmp_path):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        settings = _state_settings(tmp_path, holder.getsockname()[1])
        with pytest.raises(SystemExit, match="daemon already running"):
            run_server(settings)


def test_run_server_starts_when_the_state_pid_serves_nothing(tmp_path, monkeypatch):
    """Зомби-pid в serve.json запирал старт навсегда: `serve` видел «уже запущен»,
    а обслуживать запросы было некому."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = _state_settings(tmp_path, port)
    started = []

    async def fake_serve(passed):
        started.append(passed)

    monkeypatch.setattr("agent_dispatch.server.serve", fake_serve)
    run_server(settings)

    assert started == [settings]


def test_run_server_detects_busy_port(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        settings = Settings(
            server=ServerSettings(data_dir=tmp_path, port=port),
            executors={"codex": ExecutorSettings(adapter="codex")},
            routing=RoutingSettings(fallback_executor="codex"),
        )
        with pytest.raises(SystemExit, match="port .* is busy"):
            run_server(settings)


@pytest.mark.asyncio
async def test_lifespan_recovers_stale_task(api):
    client, dispatcher, storage, _, repo, _ = api
    record = await dispatcher.submit(DispatchRequest(task="stale", cwd=str(repo), executor="codex"))
    worker = dispatcher._tasks.pop(record.task_id)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    stale = await storage.get_task(record.task_id)
    stale.status = TaskStatus.running
    await storage.update_task(stale)
    app = client._transport.app
    async with app.router.lifespan_context(app):
        got = await dispatcher.get(record.task_id)
        assert got.status == "failed"
        assert got.result and got.result.error == "daemon restarted"


def test_port_is_free_supports_ipv6_and_reports_busy():
    import socket

    from agent_dispatch.server import port_is_free

    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
        probe.bind(("::1", 0))
        free_port = probe.getsockname()[1]
    assert port_is_free("::1", free_port) is True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        busy_port = holder.getsockname()[1]
        assert port_is_free("127.0.0.1", busy_port) is False
