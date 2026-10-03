"""Исчерпанный лимит: остывание до сброса, на всю группу, через перезапуск демона.

Строки ошибок взяты из живой телеметрии 2026-10-02: claude/opus 14 раз за день
получил недельный лимит, потому что остывание его не узнавало и длилось 15 минут.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agent_dispatch.api import create_app
from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest, ExecutionResult
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter

WAIT = 2.0
WEEKLY = "You've hit your weekly limit · resets 5am (Asia/Yekaterinburg)"


def _settings(tmp_path: Path) -> Settings:
    executors = {
        "claude/opus": ExecutorSettings(adapter="claude", model="opus"),
        "claude/haiku": ExecutorSettings(adapter="claude", model="haiku"),
        "claude/off": ExecutorSettings(adapter="claude", model="sonnet", enabled=False),
        "codex/sol": ExecutorSettings(adapter="codex", model="sol"),
        "opencode/a": ExecutorSettings(adapter="opencode", model="x5/a"),
        "opencode/b": ExecutorSettings(adapter="opencode", model="x5/b"),
    }
    return Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor="codex/sol", review_only={}),
        executors=executors,
    )


def _failed(executor: str, error: str) -> ExecutionResult:
    return ExecutionResult(status="failed", executor=executor, model=None, summary="", error=error)


async def _make(tmp_path: Path, results: dict[str, ExecutionResult] | None = None):
    settings = _settings(tmp_path)
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {
        name: FakeAdapter(name, result=(results or {}).get(name))
        for name, item in settings.executors.items()
        if item.enabled
    }
    availability = AvailabilityCache(adapters, 60)
    dispatcher = Dispatcher(settings, storage, adapters, availability, [FakeRouter()])
    return settings, storage, adapters, availability, dispatcher


async def _run(dispatcher: Dispatcher, repo: Path, executor: str, **kwargs):
    req = DispatchRequest(
        task="fix", cwd=str(repo), executor=executor, allow_escalation=False, **kwargs
    )
    return await dispatcher.wait((await dispatcher.submit(req)).task_id, WAIT)


@pytest.mark.asyncio
async def test_weekly_limit_takes_the_whole_claude_account_out_until_the_reset(tmp_path, git_repo):
    _, storage, adapters, availability, dispatcher = await _make(
        tmp_path, {"claude/opus": _failed("claude/opus", WEEKLY)}
    )
    done = await _run(dispatcher, git_repo, "claude/opus")

    cooling = availability.cooldowns()
    assert set(cooling) == {"claude/opus", "claude/haiku"}
    assert cooling["claude/haiku"].origin == "claude/opus"
    # До 5 утра по Екатеринбургу (00:00 UTC) плюс запас, а не 15 минут.
    until = cooling["claude/opus"].until_utc
    assert until.astimezone(UTC).time().hour == 0 and until.minute == 1
    assert done.result.meta["cooldown_until"] == until.isoformat(timespec="seconds")
    events = await storage.list_events(done.task_id)
    event = next(e["payload"] for e in events if e["kind"] == "cooldown")
    assert event["executors"] == ["claude/haiku", "claude/opus"]

    # Соседняя модель того же аккаунта не запускается и не жжёт попытку.
    again = await _run(dispatcher, git_repo, "claude/haiku")
    assert again.status == "failed" and again.result.meta["guard"] == "unavailable"
    assert "cooldown until" in again.result.error and "weekly limit" in again.result.error
    assert adapters["claude/haiku"].calls == []
    # Другие аккаунты в ротации.
    assert (await _run(dispatcher, git_repo, "codex/sol")).status == "completed"


@pytest.mark.asyncio
async def test_model_overload_cools_only_that_model(tmp_path, git_repo):
    _, _, _, availability, dispatcher = await _make(
        tmp_path, {"opencode/a": _failed("opencode/a", "Selected model is at capacity.")}
    )
    await _run(dispatcher, git_repo, "opencode/a")
    assert set(availability.cooldowns()) == {"opencode/a"}


@pytest.mark.asyncio
async def test_opencode_limit_group_is_the_provider(tmp_path, git_repo):
    _, _, _, availability, dispatcher = await _make(
        tmp_path, {"opencode/a": _failed("opencode/a", "insufficient_quota")}
    )
    await _run(dispatcher, git_repo, "opencode/a")
    assert set(availability.cooldowns()) == {"opencode/a", "opencode/b"}


@pytest.mark.asyncio
async def test_cooldown_survives_a_daemon_restart(tmp_path, git_repo):
    _, storage, _, _, dispatcher = await _make(
        tmp_path, {"claude/opus": _failed("claude/opus", WEEKLY)}
    )
    await _run(dispatcher, git_repo, "claude/opus")
    await dispatcher.shutdown()
    await storage.close()

    # Новый демон: пустой кеш, та же база.
    _, storage, adapters, availability, dispatcher = await _make(tmp_path)
    assert availability.cooldowns() == {}
    assert await dispatcher.restore_cooldowns() == 2
    assert set(availability.cooldowns()) == {"claude/opus", "claude/haiku"}
    again = await _run(dispatcher, git_repo, "claude/opus")
    assert again.result.meta["guard"] == "unavailable" and adapters["claude/opus"].calls == []


@pytest.mark.asyncio
async def test_clearing_a_cooldown_returns_the_executor_and_forgets_it_on_disk(tmp_path, git_repo):
    _, storage, adapters, availability, dispatcher = await _make(
        tmp_path, {"claude/opus": _failed("claude/opus", WEEKLY)}
    )
    await _run(dispatcher, git_repo, "claude/opus")
    assert await dispatcher.clear_cooldowns(["claude/haiku"]) == ["claude/haiku"]
    assert set(availability.cooldowns()) == {"claude/opus"}
    assert [row["executor"] for row in await storage.active_cooldowns()] == ["claude/opus"]
    assert (await _run(dispatcher, git_repo, "claude/haiku")).status == "completed"

    assert await dispatcher.clear_cooldowns() == ["claude/opus"]
    assert await storage.active_cooldowns() == []


@pytest.mark.asyncio
async def test_api_shows_and_clears_cooldowns(tmp_path, git_repo):
    settings, storage, _, availability, dispatcher = await _make(
        tmp_path, {"claude/opus": _failed("claude/opus", WEEKLY)}
    )
    await _run(dispatcher, git_repo, "claude/opus")
    app = create_app(settings, dispatcher, storage, availability, "secret")
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        listed = (await client.get("/executors", headers=headers)).json()
        rows = {row["name"]: row for row in listed}
        assert rows["claude/haiku"]["cooldown"]["origin"] == "claude/opus"
        assert rows["claude/haiku"]["available"] is False
        assert rows["codex/sol"]["cooldown"] is None
        response = await client.delete(
            "/executors/cooldowns", params={"name": ["claude/opus"]}, headers=headers
        )
        assert response.json() == {"cleared": ["claude/opus"]}
        response = await client.delete("/executors/cooldowns", headers=headers)
        assert response.json() == {"cleared": ["claude/haiku"]}


@pytest.mark.asyncio
async def test_recheck_executor_comes_back_before_the_announced_reset(tmp_path, git_repo):
    _, _, _, availability, dispatcher = await _make(
        tmp_path,
        {"codex/sol": _failed("codex/sol", "You've hit your usage limit. Try again in 5 hours")},
    )
    dispatcher.settings.executors["codex/sol"].limit_reset = "recheck"
    await _run(dispatcher, git_repo, "codex/sol")
    left = availability.cooldown("codex/sol").until_utc - datetime.now(UTC)
    assert timedelta(minutes=59) < left <= timedelta(hours=1)
