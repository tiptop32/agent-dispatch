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
from tests.test_escalation import SPEND_LIMIT
from tests.test_mcp_server import call, view
from tests.test_mcp_server import settings as mcp_settings

WAIT = 2.0
X5, X5_FAST, SOL = "omp/x5", "omp/x5-fast", "omp/sol"
X5_MID = "omp/x5-mid"
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

    # Текст продовой задачи роутеру не уходит: выбор статический внутри периметра.
    assert not router.candidates
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

    assert not router.candidates
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
    assert not router.candidates
    assert routed.decision.executor != SOL and routed.decision.meta["perimeter"] == "prod"
    assert not adapters[SOL].calls
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_nested_prod_without_corporate_executors_names_the_perimeter_once(
    tmp_path, git_repo, corporate_perimeter
):
    """Наследник prod без доступных корпоративных: одна фраза о периметре, не две."""
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, router, dispatcher = await _dispatcher(tmp_path, settings)
    gate = asyncio.Event()
    adapters[X5].gate = gate
    parent = await dispatcher.submit(_request(git_repo, executor=X5))
    await asyncio.wait_for(adapters[X5].started.wait(), WAIT)
    for name in (X5, X5_FAST):
        dispatcher.availability.trip(name, "rate_limit", "test", 600)

    child = await _nested(dispatcher, adapters, git_repo, parent.task_id, None)
    gate.set()
    await dispatcher.wait(parent.task_id, WAIT)

    assert child.status == "failed" and child.result.meta["guard"] == "unavailable"
    assert child.result.error.count("corporate perimeter") == 1
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


def _work_then_review(adapter: FakeAdapter, work: ExecutionResult, review: ExecutionResult) -> None:
    """Один исполнитель делает правку и сам же её ревьюит вторым запуском."""

    async def pick(ctx) -> None:
        adapter.result = review if ctx.read_only else work

    adapter.on_execute = pick


def _prod_work(summary: str = "done", **kwargs) -> ExecutionResult:
    return ExecutionResult(
        status="completed",
        executor=X5,
        model=None,
        summary=summary,
        changed_files=["a.py"],
        **kwargs,
    )


@PERIMETER
@pytest.mark.asyncio
async def test_prod_review_goes_to_the_corporate_reviewer(tmp_path, git_repo, corporate_perimeter):
    """`perimeter_review_by` указывает на себя: ревьюит второй запуск x5, sol не зовётся."""
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    settings.executors[X5].review_by = SOL
    settings.executors[X5].perimeter_review_by = X5
    storage, adapters, _, dispatcher = await _dispatcher(tmp_path, settings)
    review = ExecutionResult(
        status="completed", executor=X5, model=None, summary="looks right", verdict="pass"
    )
    _work_then_review(adapters[X5], _prod_work(), review)

    task = await dispatcher.submit(_request(git_repo, executor=X5))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.status == "completed"
    assert done.result.meta["review"]["by"] == X5
    assert done.result.meta["review"]["verdict"] == "pass"
    assert not adapters[SOL].calls
    work_ctx, review_ctx = adapters[X5].calls
    assert not work_ctx.read_only and review_ctx.read_only
    # Правило данных у исполнителя и у ревьюера, хотя флага `prod` у ревью нет.
    assert "# Prod data rule" in work_ctx.prompt and "# Prod data rule" in review_ctx.prompt
    child = await storage.get_task(done.result.meta["review"]["task_id"])
    assert not child.request.prod and child.decision.meta["perimeter"] == "prod"
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_only_perimeter_review_by_reviews_prod_work(tmp_path, git_repo, corporate_perimeter):
    """Конфиг владельца из Post-Completion: только `perimeter_review_by`, без `review_by`."""
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    settings.executors[X5].perimeter_review_by = X5
    storage, adapters, _, dispatcher = await _dispatcher(tmp_path, settings)
    review = ExecutionResult(
        status="completed", executor=X5, model=None, summary="looks right", verdict="pass"
    )
    _work_then_review(adapters[X5], _prod_work(), review)

    task = await dispatcher.submit(_request(git_repo, executor=X5))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.result.meta["review"]["by"] == X5
    assert done.result.meta["review"]["verdict"] == "pass"
    assert len(adapters[X5].calls) == 2
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_only_perimeter_review_by_skips_work_outside_the_perimeter(
    tmp_path, git_repo, corporate_perimeter
):
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    settings.executors[X5].perimeter_review_by = X5
    storage, adapters, _, dispatcher = await _dispatcher(
        tmp_path, settings, results={X5: _prod_work()}
    )

    task = await dispatcher.submit(_request(git_repo, executor=X5, prod=False))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.status == "completed"
    assert "review" not in done.result.meta
    assert len(adapters[X5].calls) == 1
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_prod_work_without_any_reviewer_setting_is_marked_skipped(
    tmp_path, git_repo, corporate_perimeter
):
    """Поставляемый конфиг: оба ключа закомментированы. Пропуск ревью виден в результате."""
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    storage, adapters, _, dispatcher = await _dispatcher(
        tmp_path, settings, results={X5: _prod_work()}
    )

    task = await dispatcher.submit(_request(git_repo, executor=X5))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.status == "completed"
    assert done.result.meta["review"]["verdict"] == "skipped"
    assert done.result.meta["review"]["reason"] == "perimeter: no corporate reviewer"
    assert len(adapters[X5].calls) == 1
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_prod_review_without_corporate_reviewer_is_skipped(
    tmp_path, git_repo, corporate_perimeter
):
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    settings.executors[X5].review_by = SOL
    storage, adapters, _, dispatcher = await _dispatcher(
        tmp_path, settings, results={X5: _prod_work()}
    )

    task = await dispatcher.submit(_request(git_repo, executor=X5))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert done.status == "completed"
    review = done.result.meta["review"]
    assert review["verdict"] == "skipped"
    assert review["reason"] == "perimeter: no corporate reviewer"
    # Внешнего `review_by` не звали: называть его ревьюером было бы неправдой.
    assert review["by"] == ""
    # Внешний ревьюер не ставится вовсе, а не отказывается guard'ом.
    assert review.get("task_id") is None
    assert not adapters[SOL].calls and len(adapters[X5].calls) == 1
    assert not [t for t in await storage.list_tasks() if t.parent_task_id == task.task_id]
    await storage.close()


@pytest.mark.asyncio
async def test_prod_report_is_redacted_before_it_is_stored(tmp_path, git_repo):
    settings = _settings(tmp_path, corporate_perimeter=True)
    work = _prod_work(
        summary=(
            "checked 42 orders; db "
            "postgres://svc:hunter2@db.example:5432/orders"  # pragma: allowlist secret
        ),
        error="curl failed: Authorization: Bearer abcdef1234567890",
    )
    storage, _, _, dispatcher = await _dispatcher(tmp_path, settings, results={X5: work})

    task = await dispatcher.submit(_request(git_repo, executor=X5))
    await dispatcher.wait(task.task_id, WAIT)
    stored = await storage.get_task(task.task_id)

    assert "hunter2" not in stored.result.summary and "checked 42 orders" in stored.result.summary
    assert "abcdef1234567890" not in stored.result.error  # pragma: allowlist secret
    assert stored.result.meta["redacted"] == {"replacements": 2, "fields": ["summary", "error"]}
    await storage.close()


@pytest.mark.asyncio
async def test_report_outside_prod_is_not_redacted(tmp_path, git_repo):
    settings = _settings(tmp_path, corporate_perimeter=True)
    summary = "local test db postgres://dev:dev@localhost/test"  # pragma: allowlist secret
    storage, _, _, dispatcher = await _dispatcher(
        tmp_path, settings, results={X5: _prod_work(summary=summary)}
    )

    task = await dispatcher.submit(_request(git_repo, executor=X5, prod=False))
    stored = await dispatcher.wait(task.task_id, WAIT)

    assert stored.result.summary == summary and "redacted" not in stored.result.meta
    await storage.close()


def test_perimeter_reviewer_must_be_corporate(tmp_path):
    settings = _settings(tmp_path, corporate_perimeter=True)
    data = settings.model_dump()
    data["executors"][X5]["perimeter_review_by"] = X5
    assert Settings.model_validate(data).executors[X5].perimeter_review_by == X5
    data["executors"][X5]["perimeter_review_by"] = SOL
    with pytest.raises(
        ValueError, match=r"perimeter_review_by: 'omp/sol' is not `corporate: true`"
    ):
        Settings.model_validate(data)
    data["executors"][X5]["perimeter_review_by"] = "ghost"
    with pytest.raises(ValueError, match="perimeter_review_by: unknown executor 'ghost'"):
        Settings.model_validate(data)


async def _silent_routers(tmp_path: Path, corporate_perimeter: bool, results=None):
    """Три корпоративных исполнителя и два роутера (как Jev и `claude_local`).

    Ни один роутер не должен получить текст продовой задачи: у любого шага
    линии остаётся не меньше двух корпоративных кандидатов, поэтому короткий
    путь «один кандидат» здесь не срабатывает.
    """
    settings = _settings(tmp_path, corporate_perimeter=corporate_perimeter)
    settings.executors[X5_MID] = ExecutorSettings(
        adapter="omp", model="x5", tier="balanced", corporate=True
    )
    storage, adapters, router, dispatcher = await _dispatcher(tmp_path, settings, results)
    routers = [router, _Router()]
    dispatcher.routers = routers
    return storage, adapters, routers, dispatcher


@PERIMETER
@pytest.mark.asyncio
async def test_prod_root_task_never_reaches_a_router(tmp_path, git_repo, corporate_perimeter):
    storage, adapters, routers, dispatcher = await _silent_routers(tmp_path, corporate_perimeter)

    task = await dispatcher.submit(_request(git_repo))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert not any(router.candidates for router in routers)
    # Статический тир `balanced`, как у плеча `explore`.
    assert done.status == "completed" and done.decision.executor == X5_MID
    assert done.decision.router == RouterKind.fallback
    assert done.decision.meta["perimeter"] == "prod"
    assert any("prod: routers skipped" in n for n in done.decision.meta["selection_notes"])
    assert not adapters[SOL].calls
    await storage.close()


@pytest.mark.asyncio
async def test_prod_static_choice_respects_max_capability(tmp_path, git_repo):
    storage, _, routers, dispatcher = await _silent_routers(tmp_path, False)

    task = await dispatcher.submit(_request(git_repo, max_capability="fast"))
    done = await dispatcher.wait(task.task_id, WAIT)

    assert not any(router.candidates for router in routers)
    assert done.decision.executor == X5_FAST and done.decision.capability == "fast"
    assert done.decision.meta["capability_uncapped"] == "balanced"
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_prod_reroute_after_executor_failure_never_reaches_a_router(
    tmp_path, git_repo, corporate_perimeter
):
    failed = ExecutionResult(
        status="failed", executor=X5, model=None, summary="", error=SPEND_LIMIT
    )
    storage, adapters, routers, dispatcher = await _silent_routers(
        tmp_path, corporate_perimeter, results={X5: failed}
    )
    dispatcher.settings.escalation = {}

    first = await dispatcher.submit(_request(git_repo, executor=X5))
    done = await dispatcher.wait(first.task_id, WAIT)

    assert done.task_id != first.task_id and done.status == "completed"
    assert done.decision.executor in {X5_FAST, X5_MID}
    assert not any(router.candidates for router in routers)
    assert not adapters[SOL].calls
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_prod_followup_never_reaches_a_router(tmp_path, git_repo, corporate_perimeter):
    storage, adapters, routers, dispatcher = await _silent_routers(tmp_path, corporate_perimeter)
    first = await dispatcher.submit(_request(git_repo, executor=X5))
    assert (await dispatcher.wait(first.task_id, WAIT)).status == "completed"
    dispatcher.availability.trip(X5, "rate_limit", "test", 600)

    record = await dispatcher.followup(first.task_id, FollowupRequest(message="more"))
    done = await dispatcher.wait(record.task_id, WAIT)

    assert not any(router.candidates for router in routers)
    assert done.decision.executor == X5_MID and done.decision.meta["perimeter"] == "prod"
    assert not adapters[SOL].calls
    await storage.close()


@PERIMETER
@pytest.mark.asyncio
async def test_prod_nested_task_never_reaches_a_router(tmp_path, git_repo, corporate_perimeter):
    storage, adapters, routers, dispatcher = await _silent_routers(tmp_path, corporate_perimeter)
    gate = asyncio.Event()
    adapters[X5].gate = gate
    parent = await dispatcher.submit(_request(git_repo, executor=X5))
    await asyncio.wait_for(adapters[X5].started.wait(), WAIT)

    routed = await _nested(dispatcher, adapters, git_repo, parent.task_id, None)
    gate.set()
    await dispatcher.wait(parent.task_id, WAIT)

    assert not any(router.candidates for router in routers)
    assert routed.decision.executor == X5_MID and routed.decision.meta["perimeter"] == "prod"
    await storage.close()


@pytest.mark.asyncio
async def test_nested_route_only_inherits_prod(tmp_path, git_repo):
    """Вложенный MCP `route` из продовой задачи без своего `prod`.

    При `corporate_perimeter: false` без наследования роутер увидел бы
    внешнего исполнителя и текст задачи.
    """
    storage, adapters, routers, dispatcher = await _silent_routers(tmp_path, False)
    gate = asyncio.Event()
    adapters[X5].gate = gate
    parent = await dispatcher.submit(_request(git_repo, executor=X5))
    await asyncio.wait_for(adapters[X5].started.wait(), WAIT)

    decision, _ = await dispatcher.route_only(
        DispatchRequest(task="nested", cwd=str(git_repo), parent_task_id=parent.task_id, hop=1)
    )
    gate.set()
    await dispatcher.wait(parent.task_id, WAIT)

    assert not any(router.candidates for router in routers)
    assert decision.executor != SOL and decision.meta["perimeter"] == "prod"
    await storage.close()


@pytest.mark.asyncio
async def test_route_only_inherits_the_answered_perimeter(tmp_path, git_repo):
    """Наследник задачи под периметром по ответу Jev: роутер видит только корпоративных."""
    storage, adapters, routers, dispatcher = await _silent_routers(tmp_path, True)
    parent = await dispatcher.submit(DispatchRequest(task="x", cwd=str(git_repo), executor=X5))
    done = await dispatcher.wait(parent.task_id, WAIT)
    record = await storage.get_task(done.task_id)
    record.decision = record.decision.model_copy(update={"meta": {"perimeter": "answered"}})
    await storage.update_task(record)

    decision, _ = await dispatcher.route_only(
        DispatchRequest(task="nested", cwd=str(git_repo), parent_task_id=parent.task_id, hop=1)
    )

    assert routers[0].candidates == [[X5, X5_FAST, X5_MID]]
    assert decision.executor != SOL
    await storage.close()


@pytest.mark.asyncio
async def test_nested_route_only_refuses_an_explicit_external_executor(tmp_path, git_repo):
    storage, _, _, dispatcher = await _silent_routers(tmp_path, False)
    parent = await dispatcher.submit(_request(git_repo, executor=X5))
    await dispatcher.wait(parent.task_id, WAIT)

    _, events = await dispatcher.route_only(
        DispatchRequest(
            task="nested", cwd=str(git_repo), executor=SOL, parent_task_id=parent.task_id, hop=1
        )
    )

    assert [event.reason for event in events] == ["perimeter"]
    assert "ancestor prod task" in events[0].detail
    await storage.close()
