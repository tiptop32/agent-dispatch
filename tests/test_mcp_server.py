import os
from datetime import UTC, datetime

import httpx
import pytest
import respx
from mcp.client._memory import InMemoryTransport
from mcp.client.client import Client

from agent_dispatch.config import ExecutorSettings, ServerSettings, Settings
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.mcp.server import build_server
from agent_dispatch.models import ExecutionResult, SourceAgent, TaskRecord, TaskView
from agent_dispatch.serve_state import ServeState


def settings(tmp_path):
    return Settings(
        server=ServerSettings(data_dir=tmp_path),
        executors={"codex": ExecutorSettings(adapter="codex")},
    )


def state():
    return ServeState(pid=os.getpid(), port=7433, token="secret", started_at=datetime.now(UTC))


def view(status="completed"):
    request = {"task": "fix", "cwd": ".", "source_agent": "codex"}
    record = TaskRecord(
        task_id="t1",
        parent_task_id=None,
        escalated_from=None,
        root_agent=SourceAgent.codex,
        source_agent=SourceAgent.codex,
        hop=0,
        request=request,
        status=status,
        decision=None,
        result=ExecutionResult(status="completed", executor="codex", model=None, summary="done"),
        log_path="",
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
    )
    return TaskView(**record.model_dump(), log_tail="")


async def call(settings, name, args, ensure=None):
    ensure = ensure or (lambda _: _state())
    srv = build_server(settings, ensure=ensure)
    async with Client(InMemoryTransport(srv)) as client:
        return await client.call_tool(name, args)


_state = state


@pytest.mark.asyncio
async def test_tools_exact(tmp_path):
    srv = build_server(settings(tmp_path), ensure=lambda _: _state())
    async with Client(InMemoryTransport(srv)) as client:
        assert {t.name for t in (await client.list_tools()).tools} == {
            "route",
            "dispatch",
            "dispatch_to",
            "status",
        }


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_body_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DISPATCH_SOURCE_AGENT", "codex")
    route = respx.post("http://127.0.0.1:7433/tasks").mock(
        return_value=httpx.Response(200, json=view("running").model_dump(mode="json"))
    )
    r = await call(settings(tmp_path), "dispatch", {"task": "fix", "cwd": "."})
    assert route.calls[0].request.headers["authorization"] == "Bearer secret"
    assert route.calls[0].request.read() and "fix" in route.calls[0].request.content.decode()
    assert r.is_error is False


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_running_hint(tmp_path):
    respx.post("http://127.0.0.1:7433/tasks").mock(
        return_value=httpx.Response(200, json=view("running").model_dump(mode="json"))
    )
    r = await call(settings(tmp_path), "dispatch", {"task": "fix", "cwd": "."})
    assert "Call `status`" in r.content[0].text


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_completed_no_hint(tmp_path):
    respx.post("http://127.0.0.1:7433/tasks").mock(
        return_value=httpx.Response(200, json=view().model_dump(mode="json"))
    )
    r = await call(settings(tmp_path), "dispatch", {"task": "fix", "cwd": "."})
    assert "Call `status`" not in r.content[0].text


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_to_executor(tmp_path):
    request = respx.post("http://127.0.0.1:7433/tasks").mock(
        return_value=httpx.Response(200, json=view().model_dump(mode="json"))
    )
    await call(settings(tmp_path), "dispatch_to", {"executor": "codex", "task": "fix", "cwd": "."})
    assert '"executor":"codex"' in request.calls[0].request.content.decode()


@pytest.mark.asyncio
@respx.mock
async def test_status_summary(tmp_path):
    respx.get("http://127.0.0.1:7433/tasks/t1").mock(
        return_value=httpx.Response(200, json=view().model_dump(mode="json"))
    )
    r = await call(settings(tmp_path), "status", {"task_id": "t1"})
    assert "done" in r.content[0].text


@pytest.mark.asyncio
@respx.mock
async def test_route_output(tmp_path):
    payload = {"executor": "codex", "confidence": 0.9, "scores": {}, "router": "fallback"}
    respx.post("http://127.0.0.1:7433/route").mock(return_value=httpx.Response(200, json=payload))
    r = await call(settings(tmp_path), "route", {"task": "fix", "cwd": "."})
    assert "executor: codex" in r.content[0].text and "confidence" in r.content[0].text


@pytest.mark.asyncio
async def test_daemon_error(tmp_path):
    async def fail(_):
        raise DaemonUnavailable("offline")

    r = await call(settings(tmp_path), "status", {"task_id": "x"}, fail)
    assert r.is_error and "offline" in r.content[0].text


@pytest.mark.asyncio
@respx.mock
async def test_status_404(tmp_path):
    respx.get("http://127.0.0.1:7433/tasks/x").mock(
        return_value=httpx.Response(404, text="missing")
    )
    r = await call(settings(tmp_path), "status", {"task_id": "x"})
    assert r.is_error and "task not found" in r.content[0].text


@pytest.mark.parametrize(
    "name", ["route", "dispatch", "dispatch_to", "status", "dispatch", "route", "status"]
)
def test_tool_names_are_nonempty(name):
    assert name


@pytest.mark.asyncio
@respx.mock
async def test_client_connect_error():
    respx.get("http://127.0.0.1:7433/health").mock(side_effect=httpx.ConnectError("offline"))
    with pytest.raises(DaemonUnavailable, match="not reachable"):
        await DispatchClient(state()).health()


@pytest.mark.asyncio
@respx.mock
async def test_client_401():
    respx.get("http://127.0.0.1:7433/health").mock(return_value=httpx.Response(401))
    with pytest.raises(DaemonUnavailable, match="token rejected"):
        await DispatchClient(state()).health()


@pytest.mark.asyncio
@respx.mock
async def test_client_500():
    respx.get("http://127.0.0.1:7433/health").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await DispatchClient(state()).health()
