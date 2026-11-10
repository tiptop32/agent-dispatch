import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from mcp.client._memory import InMemoryTransport
from mcp.client.client import Client

from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.mcp.server import build_server
from agent_dispatch.serve_state import ServeState


def settings_for(data_dir):
    return Settings(
        server=ServerSettings(data_dir=data_dir),
        routing=RoutingSettings(fallback_executor="omp"),
        executors={"omp": ExecutorSettings(adapter="omp", model="agent-lb/luna")},
    )


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("verbose", [False, True])
async def test_response_bytes_measure_returned_text_for_all_tools(tmp_path, verbose):
    data_dir = tmp_path / "metrics"
    settings = settings_for(data_dir)
    moment = datetime.now(UTC)
    state = ServeState(pid=os.getpid(), port=7433, token="secret-token", started_at=moment)
    decision = {
        "executor": "omp",
        "confidence": 0.9,
        "scores": {},
        "router": "fallback",
        "meta": {"selection_notes": ["готово 🛠"]},
    }
    payload = {
        "task_id": "returned-task",
        "parent_task_id": None,
        "escalated_from": None,
        "root_agent": "omp",
        "source_agent": "omp",
        "hop": 0,
        "request": {"task": "private prompt", "cwd": ".", "source_agent": "omp"},
        "status": "completed",
        "decision": decision,
        "result": {
            "status": "completed",
            "executor": "omp",
            "summary": "готово 🛠 " * 200,
            "model": None,
        },
        "log_path": "",
        "created_at": moment.isoformat(),
        "started_at": None,
        "finished_at": None,
        "log_tail": "",
    }
    respx.post("http://127.0.0.1:7433/tasks").mock(return_value=httpx.Response(200, json=payload))
    respx.get("http://127.0.0.1:7433/tasks/requested-task").mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.post("http://127.0.0.1:7433/tasks/previous-task/followup").mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.post("http://127.0.0.1:7433/route").mock(return_value=httpx.Response(200, json=decision))
    respx.get("http://127.0.0.1:7433/executors").mock(return_value=httpx.Response(200, json=[]))
    calls = [
        ("dispatch", {"task": "private prompt", "cwd": "."}),
        ("dispatch_to", {"executor": "omp", "task": "private prompt", "cwd": "."}),
        ("status", {"task_id": "requested-task"}),
        ("followup", {"task_id": "previous-task", "message": "private correction"}),
        ("route", {"task": "private prompt", "cwd": "."}),
    ]
    responses = []
    server = build_server(settings, ensure=lambda _: state)
    async with Client(InMemoryTransport(server)) as client:
        for name, args in calls:
            response = await client.call_tool(name, {**args, "verbose": verbose})
            assert response.is_error is False
            responses.append(response.content[0].text)
        await client.call_tool("executors", {})
    records = [
        json.loads(line) for line in (data_dir / "mcp_responses.jsonl").read_text().splitlines()
    ]
    assert len(records) == len(calls)
    for record, (name, _), text in zip(records, calls, responses, strict=True):
        assert set(record) == {"ts", "tool", "task_id", "bytes", "verbose"}
        assert moment <= datetime.fromisoformat(record["ts"]) <= datetime.now(UTC)
        assert record["tool"] == name
        assert record["task_id"] == (None if name == "route" else "returned-task")
        assert record["bytes"] == len(text.encode("utf-8"))
        assert record["bytes"] > len(text)
        assert record["verbose"] is verbose
    metrics = (data_dir / "mcp_responses.jsonl").read_text()
    assert "private" not in metrics and "secret-token" not in metrics and "готово" not in metrics


@pytest.mark.asyncio
@respx.mock
async def test_response_survives_metrics_permission_error(tmp_path, monkeypatch, caplog):
    settings = settings_for(tmp_path)
    state = ServeState(
        pid=os.getpid(), port=7433, token="secret-token", started_at=datetime.now(UTC)
    )
    respx.post("http://127.0.0.1:7433/route").mock(
        return_value=httpx.Response(
            200,
            json={"executor": "omp", "confidence": 0.9, "scores": {}, "router": "fallback"},
        )
    )
    original_open = Path.open
    attempts = []

    def denied(path, *args, **kwargs):
        if path == tmp_path / "mcp_responses.jsonl":
            attempts.append(path)
            raise PermissionError("sensitive filesystem details")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    server = build_server(settings, ensure=lambda _: state)
    async with Client(InMemoryTransport(server)) as client:
        response = await client.call_tool("route", {"task": "private prompt", "cwd": "."})
    assert response.is_error is False
    assert response.content[0].text.startswith("executor: omp\nroute: fallback")
    assert len(attempts) == 1
    assert "sensitive filesystem details" not in caplog.text
