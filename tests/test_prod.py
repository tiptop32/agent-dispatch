"""Флаг `prod`: вся линия задачи остаётся у исполнителей `corporate: true`.

Прод закрыт всегда, в том числе при `routing.corporate_perimeter: false`
(так он выключен у владельца), поэтому почти все тесты гоняются с обоими
значениями.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from agent_dispatch.cli import app
from agent_dispatch.config import ExecutorSettings, RoutingSettings, ServerSettings, Settings
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import (
    DispatchRequest,
    ExecutionResult,
    FollowupRequest,
    RouteDecision,
    RouterKind,
)
from agent_dispatch.routing.capability import perimeter_required
from agent_dispatch.routing.jev import JevRouter
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter
from tests.test_cli import _patch_autostart
from tests.test_cli import _state as cli_state
from tests.test_cli import _view as cli_view
from tests.test_mcp_server import call, view
from tests.test_mcp_server import settings as mcp_settings

WAIT = 2.0
X5, X5_FAST, SOL = "omp/x5", "omp/x5-fast", "omp/sol"
PERIMETER = pytest.mark.parametrize("corporate_perimeter", [True, False], ids=["on", "off"])


def _settings(tmp_path: Path, *, corporate_perimeter: bool, corporate: bool = True) -> Settings:
    executors = {
        SOL: ExecutorSettings(adapter="omp", model="sol", tier="strong"),
        X5: ExecutorSettings(adapter="omp", model="x5", tier="strong", corporate=corporate),
        X5_FAST: ExecutorSettings(adapter="omp", model="x5", tier="fast", corporate=corporate),
    }
    return Settings(
        server=ServerSettings(data_dir=tmp_path),
        routing=RoutingSettings(fallback_executor=SOL, corporate_perimeter=corporate_perimeter),
        executors=executors,
        escalation={X5: [SOL]},
    )


class _Router:
    """Роутер, который выбирает внешнего исполнителя и запоминает кандидатов."""

    name = "recording"

    def __init__(self, executor: str = SOL):
        self.executor, self.candidates = executor, []

    async def decide(self, req, candidates):
        self.candidates.append(sorted(candidates))
        executor = self.executor if self.executor in candidates else next(iter(candidates))
        return RouteDecision(
            router=RouterKind.jev,
            executor=executor,
            confidence=0.99,
            scores={executor: 0.99},
        )


async def _dispatcher(tmp_path: Path, settings: Settings, results=None, storage=None):
    storage = storage or Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {
        name: FakeAdapter(name, result=(results or {}).get(name)) for name in settings.executors
    }
    router = _Router()
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    return storage, adapters, router, dispatcher


def _request(repo: Path, **updates) -> DispatchRequest:
    return DispatchRequest(task="check prod", cwd=str(repo), **{"prod": True, **updates})


def _failed(executor: str) -> ExecutionResult:
    return ExecutionResult(status="failed", executor=executor, model=None, summary="failed")


def test_perimeter_required_for_prod_even_when_perimeter_is_off():
    pool = {SOL: ExecutorSettings(adapter="omp", model="sol")}
    verdict = perimeter_required(
        pool, perimeter=False, corporate=None, min_confidence=0.0, prod=True
    )
    assert verdict.required and verdict.status == "prod"


@PERIMETER
@pytest.mark.asyncio
async def test_router_sees_only_corporate_executors(tmp_path, git_repo, corporate_perimeter):
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, router, dispatcher = await _dispatcher(tmp_path, settings)

    task = await dispatcher.submit(_request(git_repo))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert router.candidates == [[X5, X5_FAST]]
    assert done.status == "completed" and done.decision.executor in {X5, X5_FAST}
    assert done.decision.meta["perimeter"] == "prod"
    assert not adapters[SOL].calls
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_route_only_sees_only_corporate_executors(tmp_path, git_repo, corporate_perimeter):
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, _, router, dispatcher = await _dispatcher(tmp_path, settings)

    decision, _ = await dispatcher.route_only(_request(git_repo))

    assert router.candidates == [[X5, X5_FAST]]
    assert decision.executor != SOL and decision.meta["perimeter"] == "prod"
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_escalation_skips_the_external_link(tmp_path, git_repo, corporate_perimeter):
    """Цепочка `x5 → sol`: внешнее звено пропущено, эскалация остановлена периметром."""
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, _, dispatcher = await _dispatcher(
        tmp_path, settings, results={X5: _failed(X5)}
    )

    task = await dispatcher.submit(_request(git_repo, executor=X5))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.task_id == task.task_id and done.status == "failed"
    assert done.result.meta["escalation_stopped"] == "perimeter"
    assert not adapters[SOL].calls
    assert len(await storage.list_tasks(limit=10)) == 1
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_explicit_external_executor_is_refused(tmp_path, git_repo, corporate_perimeter):
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, _, dispatcher = await _dispatcher(tmp_path, settings)

    task = await dispatcher.submit(_request(git_repo, executor=SOL))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.status == "failed" and done.result.meta["guard"] == "perimeter"
    assert "prod" in done.result.error and SOL in done.result.error
    assert not adapters[SOL].calls
    await storage.close()


@pytest.mark.asyncio
async def test_explicit_corporate_executor_runs_and_marks_the_line(tmp_path, git_repo):
    settings = _settings(tmp_path, corporate_perimeter=False)
    storage, adapters, _, dispatcher = await _dispatcher(tmp_path, settings)

    task = await dispatcher.submit(_request(git_repo, executor=X5))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.status == "completed" and adapters[X5].calls
    assert done.decision.meta["perimeter"] == "prod"
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_refused_without_corporate_executors_in_config(
    tmp_path, git_repo, corporate_perimeter
):
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter, corporate=False)
    storage, adapters, router, dispatcher = await _dispatcher(tmp_path, settings)

    task = await dispatcher.submit(_request(git_repo))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.status == "failed" and done.result.meta["guard"] == "perimeter"
    assert "no enabled executor with `corporate: true`" in done.result.error
    assert not router.candidates
    assert not any(adapter.calls for adapter in adapters.values())
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_followup_to_router_stays_inside(tmp_path, git_repo, corporate_perimeter):
    """Исполнитель остыл: follow-up идёт роутеру, но только среди корпоративных."""
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, router, dispatcher = await _dispatcher(tmp_path, settings)
    first = await dispatcher.submit(_request(git_repo, executor=X5))
    assert (await dispatcher.wait(first.task_id, WAIT)).status == "completed"
    dispatcher.availability.trip(X5, "rate_limit", "test", 600)

    record = await dispatcher.followup(first.task_id, FollowupRequest(message="more"))
    done = await dispatcher.wait(record.task_id, WAIT)

    # Единственный корпоративный кандидат: роутер не нужен, ответ о данных известен.
    assert not router.candidates
    assert done.decision.executor == X5_FAST
    assert done.decision.meta["perimeter"] == "prod"
    assert not adapters[SOL].calls
    await storage.close()


async def _nested(dispatcher, adapters, repo, parent_id, executor):
    """Вложенный dispatch без флага `prod`: требование приходит от родителя."""
    child = await dispatcher.submit(
        DispatchRequest(
            task="nested", cwd=str(repo), executor=executor, parent_task_id=parent_id, hop=1
        )
    )
    return await dispatcher.wait(child.task_id, WAIT)


@PERIMETER
@pytest.mark.asyncio
async def test_nested_task_inherits_prod(tmp_path, git_repo, corporate_perimeter):
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, router, dispatcher = await _dispatcher(tmp_path, settings)
    gate = asyncio.Event()
    adapters[X5].gate = gate
    parent = await dispatcher.submit(_request(git_repo, executor=X5))
    await asyncio.wait_for(adapters[X5].started.wait(), WAIT)

    refused = await _nested(dispatcher, adapters, git_repo, parent.task_id, SOL)
    routed = await _nested(dispatcher, adapters, git_repo, parent.task_id, None)
    gate.set()
    await dispatcher.wait(parent.task_id, WAIT)

    assert refused.status == "failed" and refused.result.meta["guard"] == "perimeter"
    assert "ancestor prod task" in refused.result.error
    assert router.candidates == [[X5, X5_FAST]]
    assert routed.decision.executor != SOL and routed.decision.meta["perimeter"] == "prod"
    assert not adapters[SOL].calls
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_daemon_restart_keeps_the_requirement(tmp_path, git_repo, corporate_perimeter):
    """Новый демон над той же базой читает требование `prod` из записи предка."""
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, _, _, dispatcher = await _dispatcher(tmp_path, settings)
    parent = await dispatcher.submit(_request(git_repo, executor=X5))
    assert (await dispatcher.wait(parent.task_id, WAIT)).status == "completed"
    await storage.close()

    fresh = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, _, dispatcher = await _dispatcher(tmp_path, fresh)
    done = await _nested(dispatcher, adapters, git_repo, parent.task_id, SOL)

    assert done.status == "failed" and done.result.meta["guard"] == "perimeter"
    assert not adapters[SOL].calls
    await storage.close()


@pytest.mark.asyncio
@respx.mock
async def test_jev_does_not_ask_about_corporate_data_for_prod():
    settings = Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "k"},
            "routing": {"fallback_executor": "external"},
            "executors": {
                "external": {"adapter": "omp", "model": "sol", "tier": "fast"},
                "internal": {"adapter": "omp", "model": "x5", "tier": "fast", "corporate": True},
            },
        }
    )
    answer = {
        "capability": {
            "type": "choice",
            "choice": "fast",
            "probabilities": {"fast": 1},
            "confidence": 0.9,
        }
    }
    route = respx.post(settings.router.jev.base_url).mock(
        return_value=httpx.Response(200, json={"answers": answer})
    )
    candidates = {"internal": settings.executors["internal"]}
    async with httpx.AsyncClient() as client:
        decision = await JevRouter(settings, client).decide(
            DispatchRequest(task="x", cwd=".", prod=True), candidates
        )
    body = json.loads(route.calls[0].request.content)
    assert "corporate_data" not in body["questions"]
    assert decision.executor == "internal" and decision.meta["perimeter"] == "prod"


@pytest.mark.asyncio
@respx.mock
async def test_mcp_tools_pass_prod(tmp_path):
    tasks = respx.post("http://127.0.0.1:7433/tasks").mock(
        return_value=httpx.Response(200, json=view().model_dump(mode="json"))
    )
    route = respx.post("http://127.0.0.1:7433/route").mock(
        return_value=httpx.Response(
            200, json={"executor": "omp", "confidence": 0.9, "scores": {}, "router": "fallback"}
        )
    )
    args = {"task": "check prod", "cwd": ".", "prod": True}
    await call(mcp_settings(tmp_path), "dispatch", args)
    await call(mcp_settings(tmp_path), "dispatch_to", {"executor": "omp", **args})
    await call(mcp_settings(tmp_path), "route", args)
    await call(mcp_settings(tmp_path), "dispatch", {"task": "x", "cwd": "."})

    sent = [json.loads(item.request.content) for item in [*tasks.calls, *route.calls]]
    assert [body["prod"] for body in sent] == [True, True, False, True]


@respx.mock
@pytest.mark.parametrize("command", ["dispatch", "route"])
def test_cli_sends_prod(tmp_config_dir, monkeypatch, command):
    state = cli_state()
    _patch_autostart(monkeypatch, state)
    path = "tasks" if command == "dispatch" else "route"
    payload = (
        cli_view()
        if command == "dispatch"
        else {"executor": "omp", "confidence": 0.9, "scores": {}, "router": "fallback"}
    )
    request = respx.post(f"http://127.0.0.1:{state.port}/{path}").mock(
        return_value=httpx.Response(200, json=payload)
    )

    result = CliRunner().invoke(app, [command, "--prod", "x"])

    assert result.exit_code == 0, result.output
    assert json.loads(request.calls[0].request.content)["prod"] is True
