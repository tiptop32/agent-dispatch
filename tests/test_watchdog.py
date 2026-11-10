import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_dispatch.config import (
    ExecutionSettings,
    ExecutorSettings,
    RoutingSettings,
    ServerSettings,
    Settings,
)
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors import watchdog as watchdog_module
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.omp import OmpAdapter
from agent_dispatch.executors.process import ProcessOutcome
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.executors.watchdog import OmpWatchdog
from agent_dispatch.models import DispatchRequest
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeRouter

FIXTURES = Path(__file__).parent / "fixtures" / "omp"
FAKE = Path(__file__).parent / "fakes" / "omp_stream.sh"


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(watchdog_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def emit(watchdog, kind, **fields):
    watchdog.on_line(json.dumps({"type": kind, **fields}))


def test_tool_lifetime_uses_execution_events_and_separate_idle_limit(clock):
    watchdog = OmpWatchdog(300, 900, 6)
    emit(
        watchdog,
        "message_end",
        message={
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "one", "name": "bash", "arguments": {}}],
        },
    )
    clock[0] = 301
    assert watchdog.check() == {"reason": "idle", "idle_s": 301, "tool_open": False}
    emit(watchdog, "tool_execution_start", toolCallId="one", toolName="bash", args={})
    clock[0] = 602
    assert watchdog.check() is None
    emit(watchdog, "tool_execution_start", toolCallId="two", toolName="read", args={})
    emit(watchdog, "tool_execution_end", toolCallId="two", result={"content": []})
    clock[0] = 1501
    assert watchdog.check() is None
    clock[0] = 1502
    assert watchdog.check() == {"reason": "idle", "idle_s": 900, "tool_open": True}
    emit(watchdog, "tool_execution_end", toolCallId="one", result={"content": []})
    clock[0] = 1802
    assert watchdog.check() == {"reason": "idle", "idle_s": 300, "tool_open": False}


def test_loop_counts_consecutive_tool_argument_result_hashes(clock):
    watchdog = OmpWatchdog(300, 900, 6)
    for call, (name, args, result) in enumerate(
        [
            ("bash", {"command": "pytest"}, "failure"),
            ("bash", {"command": "pytest"}, "failure"),
            ("bash", {"command": "pytest"}, "changed failure"),
            ("bash", {"command": "pytest -x"}, "changed failure"),
            ("read", {"command": "pytest -x"}, "changed failure"),
            *[("bash", {"a": 1, "b": 2}, "failure")] * 5,
        ]
    ):
        emit(watchdog, "tool_execution_start", toolCallId=str(call), toolName=name, args=args)
        emit(watchdog, "tool_execution_end", toolCallId=str(call), result=result)
        assert watchdog.check() is None
    emit(
        watchdog,
        "tool_execution_start",
        toolCallId="last",
        toolName="bash",
        args={"b": 2, "a": 1},
    )
    emit(watchdog, "tool_execution_end", toolCallId="last", result="failure")
    assert watchdog.check() == {"reason": "looping", "repeats": 6, "tool_open": False}


def test_productive_call_before_next_check_resets_loop_detection(clock):
    watchdog = OmpWatchdog(300, 900, 6)
    for line in (FIXTURES / "looping.ndjson").read_text().splitlines():
        watchdog.on_line(line)
    # No check occurred while the repeated results arrived between intervals.
    clock[0] = 299
    emit(
        watchdog,
        "tool_execution_start",
        toolCallId="productive",
        toolName="bash",
        args={"command": "pytest -q"},
    )
    emit(
        watchdog,
        "tool_execution_end",
        toolCallId="productive",
        result={"content": [{"type": "text", "text": "tests passed"}]},
    )
    clock[0] = 300
    assert watchdog.check() is None


def test_different_call_started_after_loop_resets_before_it_finishes(clock):
    # Ревью 2026-11-10 (sol): шесть одинаковых вызовов, затем агент начал другой, долгий вызов.
    # Проверка до его конца не должна убивать работу как петлю: выход из петли уже начался.
    watchdog = OmpWatchdog(300, 900, 6)
    for line in (FIXTURES / "looping.ndjson").read_text().splitlines():
        watchdog.on_line(line)
    emit(
        watchdog,
        "tool_execution_start",
        toolCallId="different",
        toolName="bash",
        args={"command": "uv run pytest tests -q"},
    )
    clock[0] = 300
    assert watchdog.check() is None


def test_same_call_started_again_keeps_loop_evidence(clock):
    watchdog = OmpWatchdog(300, 900, 6)
    for line in (FIXTURES / "looping.ndjson").read_text().splitlines():
        watchdog.on_line(line)
    last = [json.loads(x) for x in (FIXTURES / "looping.ndjson").read_text().splitlines()]
    start = next(e for e in reversed(last) if e.get("type") == "tool_execution_start")
    emit(
        watchdog,
        "tool_execution_start",
        toolCallId="again",
        toolName=start["toolName"],
        args=start["args"],
    )
    assert watchdog.check()["reason"] == "looping"


def test_repeated_bash_failure_ignores_wall_clock_variation(clock):
    watchdog = OmpWatchdog(300, 900, 3)
    for call in range(3):
        emit(
            watchdog,
            "tool_execution_start",
            toolCallId=str(call),
            toolName="bash",
            args={"command": "pytest"},
        )
        emit(
            watchdog,
            "tool_execution_end",
            toolCallId=str(call),
            result={
                "content": [{"type": "text", "text": "failure"}],
                "details": {"wallTimeMs": 10.5 + call, "exitCode": 1},
            },
        )
    assert watchdog.check()["reason"] == "looping"


def test_slow_progress_165_turns_survives_periodic_watchdog(clock):
    # c0121093 shape: 165 turns at 11 s/turn and 86 productive tool calls.
    watchdog = OmpWatchdog(300, 900, 6)
    stdout = (FIXTURES / "slow_progress.ndjson").read_text()
    next_check = 300
    for line in stdout.splitlines():
        event = json.loads(line)
        if event.get("type") == "message_end":
            clock[0] = event["message"]["timestamp"] / 1000
        if clock[0] >= next_check:
            assert watchdog.check() is None
            next_check += 300
        watchdog.on_line(line)
    assert clock[0] == 1804
    assert watchdog.check() is None
    adapter = OmpAdapter("omp/test", ExecutorSettings(adapter="omp", model="test/model"))
    result = adapter._parse_result(ProcessOutcome(0, stdout, "", False, 1804000), [])
    assert result.status == "completed"
    assert result.summary == "165 productive turns"


@pytest.mark.parametrize(
    "mode,reason,tool_open",
    [
        ("silent", "idle", False),
        ("open", "idle", True),
        ("closed", "idle", False),
        ("fixture", "looping", False),
    ],
)
async def test_omp_watchdog_stops_silence_and_loops_despite_stderr(
    git_repo, tmp_path, mode, reason, tool_open
):
    adapter = OmpAdapter(
        "omp/test", ExecutorSettings(adapter="omp", command=str(FAKE), model="test/model")
    )
    result = await adapter.execute(
        RunContext(
            cwd=str(git_repo),
            timeout_seconds=5,
            idle_timeout_seconds=0.5,
            watchdog_interval_seconds=0.05,
            watchdog_tool_idle_seconds=1,
            watchdog_repeat_limit=6,
            env={
                "FAKE_PYTHON": sys.executable,
                "FAKE_STREAM_MODE": mode,
                "FAKE_STREAM_FIXTURE": str(FIXTURES / "looping.ndjson"),
            },
            log_path=tmp_path / "watchdog.log",
            task_id="watchdog",
            prompt="work",
        )
    )
    assert result.status == "failed"
    assert result.error == f"stalled: {reason}"
    diagnostics = result.meta["watchdog"]
    assert diagnostics["tool_open"] is tool_open
    if reason == "looping":
        assert diagnostics == {"reason": "looping", "repeats": 6, "tool_open": False}
    else:
        assert set(diagnostics) == {"reason", "idle_s", "tool_open"}
        assert diagnostics["idle_s"] >= (1 if tool_open else 0.5)
    assert "quiet MCP diagnostic" in (tmp_path / "watchdog.log").read_text()


@pytest.mark.parametrize("mode", ["long_tool", "fixture"])
async def test_dispatch_forwards_watchdog_limits_to_real_omp(git_repo, tmp_path, monkeypatch, mode):
    # Three completed repetitions distinguish the configured threshold from six.
    fixture = tmp_path / "three-calls.ndjson"
    fixture.write_text("\n".join((FIXTURES / "looping.ndjson").read_text().splitlines()[:7]) + "\n")
    monkeypatch.setenv("FAKE_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_STREAM_MODE", mode)
    monkeypatch.setenv("FAKE_STREAM_FIXTURE", str(fixture))
    monkeypatch.setenv("FAKE_TOOL_SECONDS", "1.3")
    settings = Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor="omp/test"),
        execution=ExecutionSettings(
            idle_timeout_seconds=1,
            watchdog_interval_seconds=0.05,
            watchdog_tool_idle_seconds=2,
            watchdog_repeat_limit=3,
            transport_retry_attempts=0,
        ),
        executors={
            "omp/test": ExecutorSettings(adapter="omp", command=str(FAKE), model="test/model")
        },
        escalation={},
    )
    adapters = {
        "omp/test": OmpAdapter("omp/test", settings.executors["omp/test"], data_dir=tmp_path)
    }
    storage = Storage(tmp_path / "watchdog.sqlite")
    await storage.open()
    dispatcher = Dispatcher(
        settings, storage, adapters, AvailabilityCache(adapters, 60), [FakeRouter()]
    )
    try:
        submitted = await dispatcher.submit(
            DispatchRequest(
                task="build",
                cwd=str(git_repo),
                executor="omp/test",
                timeout_seconds=5,
                allow_escalation=False,
            )
        )
        done = await dispatcher.wait(submitted.task_id, 10)
        assert done.result is not None
        if mode == "long_tool":
            assert done.status == "completed"
            assert done.result.summary == "build finished"
            assert "watchdog" not in done.result.meta
        else:
            assert done.status == "failed"
            assert done.result.error == "stalled: looping"
            assert done.result.meta["watchdog"] == {
                "reason": "looping",
                "repeats": 3,
                "tool_open": False,
            }
    finally:
        await dispatcher.shutdown()
        await storage.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("watchdog_interval_seconds", 0),
        ("watchdog_interval_seconds", float("inf")),
        ("watchdog_tool_idle_seconds", 0),
        ("watchdog_tool_idle_seconds", float("nan")),
        ("watchdog_repeat_limit", -1),
    ],
)
def test_watchdog_settings_reject_invalid_limits(field, value):
    with pytest.raises(ValidationError):
        ExecutionSettings(**{field: value})


def test_zero_repeat_limit_disables_loop_detection(clock):
    watchdog = OmpWatchdog(300, 900, 0)
    for line in (FIXTURES / "looping.ndjson").read_text().splitlines():
        watchdog.on_line(line)
    assert watchdog.check() is None
