"""Opt-in selection at real HTTP, storage, prompt and executor boundaries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from agent_dispatch.config import Settings
from agent_dispatch.dispatch.context_blocks import ContextSpan
from agent_dispatch.dispatch.context_compression import StepOutcome, StepStart
from agent_dispatch.dispatch.dispatcher import Dispatcher
from agent_dispatch.dispatch.followup import FollowupError
from agent_dispatch.executors.registry import AvailabilityCache
from agent_dispatch.models import DispatchRequest, ExecutionResult, FollowupRequest, RouteDecision
from agent_dispatch.telemetry.storage import Storage
from tests.fakes.adapters import FakeAdapter, FakeRouter
from tests.test_storage import make_task

URL = "https://selector.invalid/decisions"
WAIT = 5
SOURCE = "# A\r\nFirst fact.\r\n\r\n# B\r\nSecond fact. 😀\r\n"


def envelope(p=0.60, *, cost=0.125):
    return {
        "id": "generation-safe",
        "model": "response-model",
        "answers": {"delete_safe": {"type": "noul", "noul": p}},
        "usage": {"cost": cost, "input_tokens": 10, "output_tokens": 2, "is_byok": False},
    }


@pytest.fixture
async def system(tmp_path, git_repo):
    settings = Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "test-key"},  # pragma: allowlist secret
            "server": {"data_dir": str(tmp_path / "data")},
            "routing": {"fallback_executor": "omp", "corporate_perimeter": False},
            "router": {
                "external": {"trusted_roots": [str(git_repo)]},
                "jev": {"base_url": URL, "retries": 9},
            },
            "execution": {"review_fix_rounds": 0},
            "executors": {
                "omp": {"adapter": "omp", "model": "test"},
                "omp/next": {"adapter": "omp", "model": "test"},
                "omp/final": {"adapter": "omp", "model": "test"},
            },
            "escalation": {"omp": ["omp/next"], "omp/next": ["omp/final"]},
        }
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.open()
    adapters = {name: FakeAdapter(name) for name in settings.executors}
    router = FakeRouter(
        RouteDecision(executor="omp", confidence=1, scores={"omp": 1}, router="jev")
    )
    dispatcher = Dispatcher(settings, storage, adapters, AvailabilityCache(adapters, 60), [router])
    yield settings, storage, adapters, router, dispatcher
    await dispatcher.shutdown()
    await storage.close()


def request(repo, **updates):
    return DispatchRequest(
        **{
            "task": "Keep needed facts",
            "cwd": str(repo),
            "context": SOURCE,
            "context_compression": "jev",
            "executor": "omp",
            **updates,
        }
    )


async def run(dispatcher, req, **kwargs):
    submitted = await dispatcher.submit(req, **kwargs)
    return await dispatcher.wait(submitted.task_id, WAIT)


def assert_never_run(done, adapters, code):
    assert done.status in ("failed", "cancelled")
    assert done.result.meta["error_code"] == code
    assert done.result.meta["executor_started"] is False
    assert not any(adapter.calls for adapter in adapters.values())
    metrics = done.result.meta["context_compression"]
    assert metrics["output_bytes"] is None
    assert metrics["blocks_kept"] is None
    assert "kept_ids" not in metrics
    assert "escalated_to" not in done.result.meta
    assert "failure_class" not in done.result.meta
    return metrics


@pytest.mark.parametrize(
    ("probabilities", "expected"),
    [([0.60, 0.5999999999999999], "# B\r\nSecond fact. 😀\r\n"), ([1, 1], ""), ([0, 0], SOURCE)],
)
@respx.mock
async def test_selection_commits_before_http_and_changes_only_prompt_context(
    system, git_repo, probabilities, expected
):
    _, storage, adapters, _, dispatcher = system
    bodies = []

    async def respond(req):
        with sqlite3.connect(storage.path) as independent:
            status = independent.execute(
                "SELECT status FROM compression_steps ORDER BY step_index DESC LIMIT 1"
            ).fetchone()[0]
        assert status == "started"
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=envelope(probabilities[len(bodies) - 1]))

    respx.post(URL).mock(side_effect=respond)
    req = request(git_repo, constraints=["immutable constraint"], files=["a.py"])
    original = req.model_dump_json()
    done = await run(dispatcher, req)
    assert done.status == "completed", done.result
    assert req.model_dump_json() == original
    stored = await storage.get_task(done.task_id)
    assert stored.request.context == SOURCE
    assert stored.request._context_provenance == (ContextSpan(0, len(SOURCE), "user", None),)
    assert "_context_provenance" not in stored.model_dump_json()
    prompt = adapters["omp"].calls[0].prompt
    assert (SOURCE in prompt) is (expected == SOURCE)
    assert ("First fact." in prompt) is ("First fact." in expected)
    assert ("Second fact." in prompt) is ("Second fact." in expected)
    assert "immutable constraint" in prompt and "a.py" in prompt
    assert [body["state"]["candidate_id"] for body in bodies] == ["b0000", "b0001"]
    assert [b["id"] for b in bodies[1]["state"]["blocks"]] == (
        ["b0000", "b0001"] if expected == SOURCE else ["b0001"]
    )
    rows = await storage.compression_steps(done.task_id)
    assert [row["status"] for row in rows] == ["succeeded", "succeeded"]
    assert await storage.tree_cost([done.task_id]) == 0.25
    assert done.result.meta["context_compression"]["usage"]["cost"] == 0.25
    events = await storage.list_events(done.task_id)
    assert sum(event["kind"] == "executor_started" for event in events) == 1
    assert done.result.meta["executor_started"] is True


@respx.mock
async def test_default_omits_selection_and_preserves_prompt(system, git_repo):
    _, storage, adapters, _, dispatcher = system
    paid = respx.post(URL)
    done = await run(dispatcher, request(git_repo, context_compression=None))
    assert done.status == "completed"
    assert SOURCE in adapters["omp"].calls[0].prompt
    assert not paid.called
    assert await storage.compression_steps(done.task_id) == []
    assert "context_compression" not in done.result.meta


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"kind": "review"}, "compression_unsupported"),
        ({"context_mode": "prompt"}, "compression_unsupported"),
        ({"context": None}, "compression_unsupported"),
        ({"context": " \r\n\t"}, "compression_unsupported"),
        ({"context": "x" * 8193}, "compression_limit"),
        ({"context": "\ud800"}, "compression_limit"),
        ({"context": "".join(f"# {i}\ntext\n" for i in range(9))}, "compression_limit"),
        ({"task": "x" * 4097}, "compression_limit"),
    ],
)
@respx.mock
async def test_local_admission_precedes_routing(system, git_repo, updates, code):
    _, storage, adapters, router, dispatcher = system
    paid = respx.post(URL)
    done = await run(dispatcher, request(git_repo, executor=None, **updates))
    assert_never_run(done, adapters, code)
    assert router.calls == 0 and not paid.called
    assert await storage.compression_steps(done.task_id) == []


@pytest.mark.parametrize("perimeter", ["answered", "unknown", "prod"])
@respx.mock
async def test_selected_perimeter_denies_compression_and_keeps_routing_charge(
    system, git_repo, perimeter
):
    _, storage, adapters, router, dispatcher = system
    router.decision.meta["perimeter"] = perimeter
    router.decision.cost_usd = 0.05
    paid = respx.post(URL)
    done = await run(dispatcher, request(git_repo, executor=None))
    assert_never_run(done, adapters, "compression_privacy")
    assert router.calls == 1 and not paid.called
    assert await storage.cost_since(datetime.now(UTC) - timedelta(hours=1)) == 0.05


@pytest.mark.parametrize("failure", ["answer", "json", "http", "transport", "deadline"])
@respx.mock
async def test_midpass_failure_preserves_charge_without_executor_recovery(
    system, git_repo, failure, monkeypatch
):
    settings, storage, adapters, _, dispatcher = system
    calls = 0

    async def respond(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=envelope(1))
        if failure == "answer":
            return httpx.Response(200, json=envelope(True, cost=0.25))
        if failure == "json":
            return httpx.Response(200, content=b'{"answers":{},"answers":{}}')
        if failure == "http":
            return httpx.Response(503, text="PRIVATE response credential=secret")
        if failure == "transport":
            raise httpx.ConnectError("PRIVATE https://secret.invalid/key", request=req)
        settings.router.jev.timeout_seconds = 0.01
        raise httpx.ReadTimeout("PRIVATE timeout", request=req)

    respx.post(URL).mock(side_effect=respond)

    async def forbidden(*args, **kwargs):
        pytest.fail("compression failure entered executor recovery")

    for name in (
        "_plan_escalation",
        "_note_executor_failure",
        "_verify_and_review",
        "_recover_report",
    ):
        monkeypatch.setattr(dispatcher, name, forbidden)
    done = await run(dispatcher, request(git_repo, verify=["must-not-run"]))
    code = {
        "answer": "response",
        "json": "response",
        "http": "http",
        "transport": "transport",
        "deadline": "deadline",
    }[failure]
    metrics = assert_never_run(done, adapters, f"compression_{code}")
    assert metrics["completed_steps"] == 1 and metrics["attempted_steps"] == 2
    known = 0.375 if failure == "answer" else 0.125
    assert metrics["usage"]["known_cost"] == known
    assert await storage.tree_cost([done.task_id]) == known
    assert calls == 2
    assert "PRIVATE" not in json.dumps(done.result.model_dump())
    assert not await storage.active_cooldowns()
    with pytest.raises(FollowupError, match="never reached") as error:
        await dispatcher.followup(done.task_id, FollowupRequest(message="continue"))
    assert error.value.code == "no_executor_run"


@respx.mock
async def test_cancellation_closes_real_pending_row_and_survives_reopen(system, git_repo):
    _, storage, adapters, _, dispatcher = system
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def respond(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=envelope(1))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    respx.post(URL).mock(side_effect=respond)
    submitted = await dispatcher.submit(request(git_repo))
    await asyncio.wait_for(entered.wait(), WAIT)
    done = await dispatcher.cancel(submitted.task_id)
    metrics = assert_never_run(done, adapters, "compression_cancelled")
    assert cancelled.is_set() and calls == 2
    assert metrics["usage"]["known_cost"] == 0.125
    assert metrics["usage"]["cost"] is None
    await dispatcher.shutdown()
    await storage.close()
    await storage.open()
    rows = await storage.compression_steps(done.task_id)
    assert [row["status"] for row in rows] == ["succeeded", "cancelled"]
    assert await storage.compression_cost_unknown(task_ids=[done.task_id])
    assert await storage.tree_cost([done.task_id]) == 0.125


@respx.mock
async def test_cancel_after_commit_before_start_callback_returns(system, git_repo, monkeypatch):
    _, storage, adapters, _, dispatcher = system
    committed = asyncio.Event()
    original = storage.start_compression_step

    async def pause(task_id, step, *, acknowledge):
        await original(task_id, step, acknowledge=acknowledge)
        committed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(storage, "start_compression_step", pause)
    paid = respx.post(URL)
    submitted = await dispatcher.submit(request(git_repo))
    await asyncio.wait_for(committed.wait(), WAIT)
    done = await dispatcher.cancel(submitted.task_id)
    metrics = assert_never_run(done, adapters, "compression_cancelled")
    assert not paid.called
    assert metrics["attempted_steps"] == 1 and metrics["completed_steps"] == 0
    assert (await storage.compression_steps(done.task_id))[0]["status"] == "cancelled"


@pytest.mark.parametrize("cap", ["daily_cost_limit_usd", "max_chain_cost_usd"])
@pytest.mark.parametrize("cost", [0.125, None])
@respx.mock
async def test_post_step_budget_blocks_next_call_and_executor(system, git_repo, cap, cost):
    settings, storage, adapters, _, dispatcher = system
    setattr(settings.routing, cap, 0.125)
    paid = respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(1, cost=cost)))
    done = await run(dispatcher, request(git_repo))
    code = "compression_budget" if cost is not None else "compression_budget_unknown"
    metrics = assert_never_run(done, adapters, code)
    assert paid.call_count == 1
    assert metrics["completed_steps"] == 1 and metrics["attempted_steps"] == 1
    assert await storage.tree_cost([done.task_id]) == (cost or 0)


@respx.mock
async def test_unknown_usage_is_not_free_but_allowed_without_caps(system, git_repo):
    _, storage, adapters, _, dispatcher = system
    respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(0, cost=None)))
    done = await run(dispatcher, request(git_repo))
    assert done.status == "completed" and adapters["omp"].calls
    usage = done.result.meta["context_compression"]["usage"]
    assert usage["cost"] is None and usage["cost_complete"] is False
    assert await storage.compression_cost_unknown(task_ids=[done.task_id])


@respx.mock
async def test_aggregate_overflow_keeps_individual_costs_and_safe_metrics(system, git_repo):
    _, storage, adapters, _, dispatcher = system
    respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(1, cost=1e308)))
    done = await run(dispatcher, request(git_repo))
    metrics = assert_never_run(done, adapters, "compression_response")
    assert metrics["usage"]["cost"] is None and metrics["usage"]["known_cost"] is None
    assert metrics["usage"]["cost_complete"] is False
    assert [row["cost_usd"] for row in await storage.compression_steps(done.task_id)] == [1e308] * 2
    assert await storage.compression_cost_unknown(task_ids=[done.task_id])
    json.dumps(metrics, allow_nan=False)


@pytest.mark.parametrize("started", [False, True])
async def test_restart_interrupts_pending_and_reconstructs_real_start_marker(
    system, git_repo, started
):
    _, storage, _, _, dispatcher = system
    task = make_task(status="routing", assigned=True)
    task.request = request(git_repo)
    await storage.insert_task(task)
    step = StepStart(0, "b0000", ("b0000", "b0001"), datetime.now(UTC), "requested")
    await storage.start_compression_step(task.task_id, step, acknowledge=lambda: None)
    outcome = StepOutcome(
        index=0,
        candidate_id="b0000",
        status="succeeded",
        completed_at=datetime.now(UTC),
        latency_ms=1,
        jev_id="gen",
        response_model="model",
        p_delete_safe=1,
        deleted=True,
        cost_usd=0.125,
        input_tokens=10,
        output_tokens=2,
        is_byok=False,
    )
    await storage.finish_compression_step(task.task_id, outcome)
    await storage.start_compression_step(
        task.task_id, replace(step, index=1, candidate_id="b0001"), acknowledge=lambda: None
    )
    if started:
        await storage.add_event(task.task_id, "executor_started", {})
    assert await dispatcher.recover_stale() == 1
    done = await storage.get_task(task.task_id)
    assert done.result.meta["executor_started"] is started
    if not started:
        assert done.result.meta["error_code"] == "compression_interrupted"
        assert done.result.meta["context_compression"]["status"] == "interrupted"
    assert [row["status"] for row in await storage.compression_steps(task.task_id)] == [
        "succeeded",
        "interrupted",
    ]
    assert await storage.tree_cost([task.task_id]) == 0.125
    assert await dispatcher.recover_stale() == 0


@respx.mock
async def test_followup_skips_assigned_compression_failure_and_clears_opt_in(system, git_repo):
    _, storage, adapters, _, dispatcher = system
    calls = 0

    def respond(req):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=envelope(1) if calls <= 2 else {})

    respx.post(URL).mock(side_effect=respond)
    adapters["omp"].result = ExecutionResult(
        status="failed", executor="omp", model=None, summary="real earlier attempt"
    )
    root = await dispatcher.submit(request(git_repo))
    failed = await dispatcher.wait(root.task_id, WAIT)
    assert failed.escalated_from == root.task_id
    assert failed.result.meta["executor_started"] is False
    assert not adapters["omp/next"].calls
    adapters["omp"].result = None
    child = await dispatcher.followup(
        root.task_id, FollowupRequest(message="continue actual run", allow_escalation=False)
    )
    assert child.request.followup_of == root.task_id
    assert child.request.context_compression is None
    assert child.request._context_provenance is None
    assert (await dispatcher.wait(child.task_id, WAIT)).status == "completed"
    assert calls == 3
    assert (await storage.get_task(root.task_id)).request.context == SOURCE


@respx.mock
async def test_worktree_is_created_only_after_selection_then_rewrites_kept_paths(system, git_repo):
    _, storage, adapters, _, dispatcher = system
    context = f"# Drop\nunused\n# Keep\nRead {git_repo}/a.py\n"
    calls = 0

    async def respond(req):
        nonlocal calls
        calls += 1
        tasks = await storage.list_tasks()
        assert all(
            not dispatcher.settings.worktree_base.joinpath(task.task_id).exists() for task in tasks
        )
        return httpx.Response(200, json=envelope(1 if calls == 1 else 0))

    respx.post(URL).mock(side_effect=respond)
    submitted = await dispatcher.submit(
        request(git_repo, context=context, workspace_mode="worktree")
    )
    task_id = submitted.task_id
    done = await dispatcher.wait(task_id, WAIT)
    assert done.status == "completed", done.result
    ctx = adapters["omp"].calls[0]
    assert f"Read {ctx.cwd}/a.py" in ctx.prompt
    assert "unused" not in ctx.prompt
    assert (await storage.get_task(task_id)).request.context == context


@respx.mock
async def test_symlink_escape_is_denied_before_routing(system, git_repo, tmp_path):
    settings, _, adapters, router, dispatcher = system
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    link = trusted / "escape"
    link.symlink_to(git_repo, target_is_directory=True)
    settings.router.external.trusted_roots = [str(trusted)]
    paid = respx.post(URL)
    done = await run(dispatcher, request(link, executor=None))
    assert_never_run(done, adapters, "compression_privacy")
    assert router.calls == 0 and not paid.called


@pytest.mark.parametrize("defect", ["missing", "cycle", "corrupt"])
@respx.mock
async def test_durable_lineage_defects_fail_closed_before_route(system, git_repo, defect):
    _, storage, adapters, router, dispatcher = system
    ancestor = make_task(task_id="ancestor", status="completed")
    ancestor.request.parent_task_id = "absent" if defect == "missing" else "ancestor"
    if defect == "corrupt":
        ancestor.request.parent_task_id = None
    await storage.insert_task(ancestor)
    if defect == "corrupt":
        raw = json.loads(storage._db.execute("SELECT request_json FROM tasks").fetchone()[0])
        raw["_context_provenance"] = {"version": 1, "spans": [], "context_sha256": "bad"}
        storage._db.execute("UPDATE tasks SET request_json=?", (json.dumps(raw),))
        storage._db.commit()
    paid = respx.post(URL)
    done = await run(dispatcher, request(git_repo, executor=None, parent_task_id=ancestor.task_id))
    assert_never_run(done, adapters, "compression_provenance")
    assert not paid.called and router.calls == 0


@respx.mock
async def test_generated_descendant_without_provenance_is_never_user_reclassified(system, git_repo):
    _, storage, adapters, router, dispatcher = system
    ancestor = make_task(task_id="ancestor", status="completed")
    await storage.insert_task(ancestor)
    paid = respx.post(URL)
    done = await run(dispatcher, request(git_repo, executor=None), escalated_from=ancestor.task_id)
    assert_never_run(done, adapters, "compression_provenance")
    assert router.calls == 0 and not paid.called


@respx.mock
async def test_all_pinned_context_needs_no_key_or_paid_calls(system, git_repo):
    settings, storage, adapters, _, dispatcher = system
    ancestor = make_task(task_id="ancestor", status="completed")
    await storage.insert_task(ancestor)
    req = request(git_repo, context="\n\nPinned daemon fact", parent_task_id=ancestor.task_id)
    req._context_provenance = (ContextSpan(0, len(req.context), "daemon_review", ancestor.task_id),)
    settings.secrets.clear()
    paid = respx.post(URL)
    done = await run(dispatcher, req)
    assert done.status == "completed", done.result
    assert not paid.called and "Pinned daemon fact" in adapters["omp"].calls[0].prompt
    assert done.result.meta["context_compression"]["usage"] == {
        "cost": 0.0,
        "known_cost": 0.0,
        "cost_complete": True,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert await storage.compression_steps(done.task_id) == []


@respx.mock
async def test_multiple_escalations_reselect_original_user_and_preserve_typed_suffixes(
    system, git_repo
):
    _, storage, adapters, _, dispatcher = system
    bodies = []

    def respond(req):
        bodies.append(json.loads(req.content)["state"])
        return httpx.Response(200, json=envelope(1))

    respx.post(URL).mock(side_effect=respond)
    adapters["omp"].result = ExecutionResult(
        status="partial",
        executor="omp",
        model=None,
        summary="first evidence",
        error="timeout",
        changed_files=["a.py"],
    )
    adapters["omp/next"].result = ExecutionResult(
        status="failed",
        executor="omp/next",
        model=None,
        summary="second evidence",
        meta={"review": {"verdict": "fail", "by": "reviewer", "summary": "preserve exception"}},
    )
    submitted = await dispatcher.submit(request(git_repo))
    done = await dispatcher.wait(submitted.task_id, WAIT)
    assert done.status == "completed", done.result
    assert len(bodies) == 6
    expected_user_blocks = ["# A\r\nFirst fact.\r\n\r\n"] * 3
    assert [bodies[i]["blocks"][0]["text"] for i in (0, 2, 4)] == expected_user_blocks
    assert all(state["candidate_id"] in ("b0000", "b0001") for state in bodies)
    origins = ["user", "user", "daemon_handoff", "daemon_review"]
    assert [b["origin"] for b in bodies[4]["blocks"]] == origins
    root = await storage.get_task(submitted.task_id)
    middle = await storage.get_task(root.result.meta["escalated_to"])
    final = await storage.get_task(middle.result.meta["escalated_to"])
    assert root.request.context == SOURCE
    assert final.request.context.startswith(middle.request.context)
    assert len(final.request._context_provenance) == 3
    prompt = adapters["omp/final"].calls[0].prompt
    assert "First fact." not in prompt and "Second fact." not in prompt
    assert prompt.count("first evidence") == 1
    assert prompt.count("preserve exception") == 1
    rows = [await storage.compression_steps(link.task_id) for link in (root, middle, final)]
    assert [len(steps) for steps in rows] == [2, 2, 2]
    assert await storage.tree_cost([root.task_id, middle.task_id, final.task_id]) == 0.75


@pytest.mark.parametrize("cap", ["daily_cost_limit_usd", "max_chain_cost_usd"])
@respx.mock
async def test_budget_rechecked_after_waiting_before_worktree_or_executor(
    system, git_repo, cap, monkeypatch
):
    settings, storage, adapters, _, dispatcher = system
    setattr(settings.routing, cap, 1.0)
    selected = asyncio.Event()
    original_event = storage.add_event

    async def signal(task_id, kind, payload):
        result = await original_event(task_id, kind, payload)
        if kind == "context_compression" and payload["status"] == "completed":
            selected.set()
        return result

    monkeypatch.setattr(storage, "add_event", signal)
    respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(0, cost=0.1)))
    async with dispatcher._cwd_lock(str(git_repo)):
        submitted = await dispatcher.submit(request(git_repo))
        await asyncio.wait_for(selected.wait(), WAIT)
        sibling = make_task(parent=submitted.task_id, status="running")
        await storage.insert_task(sibling)
        await storage.start_compression_step(
            sibling.task_id,
            StepStart(0, "b0000", ("b0000",), datetime.now(UTC), "jev"),
            acknowledge=lambda: None,
        )
        await storage.finish_compression_step(
            sibling.task_id,
            StepOutcome(
                index=0,
                candidate_id="b0000",
                status="succeeded",
                completed_at=datetime.now(UTC),
                latency_ms=1,
                jev_id=None,
                response_model=None,
                p_delete_safe=0,
                deleted=False,
                cost_usd=0.8,
                input_tokens=1,
                output_tokens=1,
                is_byok=False,
            ),
        )
    done = await dispatcher.wait(submitted.task_id, WAIT)
    assert_never_run(done, adapters, "compression_budget")


@respx.mock
async def test_persistence_failure_after_charge_has_no_executor_fallback(
    system, git_repo, monkeypatch
):
    _, storage, adapters, _, dispatcher = system
    original = storage.finish_compression_step

    async def fail_after_commit(task_id, step):
        await original(task_id, step)
        raise OSError("PRIVATE persistence failure")

    monkeypatch.setattr(storage, "finish_compression_step", fail_after_commit)
    paid = respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(1)))
    done = await run(dispatcher, request(git_repo))
    assert_never_run(done, adapters, "compression_storage")
    assert paid.call_count == 1
    assert await storage.tree_cost([done.task_id]) == 0.125
    assert "PRIVATE" not in done.result.error


@respx.mock
async def test_automatic_review_never_inherits_opt_in(system, git_repo):
    settings, storage, adapters, _, dispatcher = system
    settings.executors["omp"].review_by = "omp/next"
    adapters["omp"].result = ExecutionResult(
        status="completed", executor="omp", model=None, summary="changed", changed_files=["a.py"]
    )
    adapters["omp/next"].result = ExecutionResult(
        status="completed", executor="omp/next", model=None, summary="reviewed", verdict="pass"
    )
    paid = respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(0)))
    done = await run(dispatcher, request(git_repo))
    assert done.status == "completed", done.result
    review = done.result.meta["review"]
    assert review["verdict"] == "pass"
    child = await storage.get_task(review["task_id"])
    assert child.request.kind == "review" and child.request.context_compression is None
    assert await storage.compression_steps(child.task_id) == []
    assert len(adapters["omp/next"].calls) == 1 and paid.call_count == 2


@respx.mock
async def test_resumed_followup_reuses_session_without_another_selection(system, git_repo):
    _, storage, adapters, _, dispatcher = system
    adapters["omp"].result = ExecutionResult(
        status="completed",
        executor="omp",
        model=None,
        summary="finished",
        meta={"session_id": "real-session"},
    )
    paid = respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(0)))
    first = await run(dispatcher, request(git_repo))
    adapters["omp"].result = None
    child = await dispatcher.followup(first.task_id, FollowupRequest(message="one more fact"))
    done = await dispatcher.wait(child.task_id, WAIT)
    assert done.status == "completed"
    assert adapters["omp"].calls[-1].resume_session == "real-session"
    assert child.request.context_compression is None
    assert child.request._context_provenance is None
    assert await storage.compression_steps(child.task_id) == []
    assert paid.call_count == 2


@pytest.mark.parametrize("restart", [False, True])
@respx.mock
async def test_corrupt_root_provenance_is_terminal_and_preserved(system, git_repo, restart):
    _, storage, adapters, router, dispatcher = system
    task = make_task()
    task.request = request(git_repo, executor=None)
    await storage.insert_task(task)
    raw = json.loads(storage._db.execute("SELECT request_json FROM tasks").fetchone()[0])
    raw["_context_provenance"] = {"version": 1, "context_sha256": "bad", "spans": []}
    storage._db.execute("UPDATE tasks SET request_json=?", (json.dumps(raw),))
    storage._db.commit()
    paid = respx.post(URL)
    if restart:
        assert await dispatcher.recover_stale() == 1
    else:
        await dispatcher._run(task.task_id)
    done = await storage.get_task(task.task_id)
    assert_never_run(done, adapters, "compression_provenance")
    assert not paid.called and router.calls == 0
    stored = json.loads(storage._db.execute("SELECT request_json FROM tasks").fetchone()[0])
    assert stored == raw


@respx.mock
async def test_complete_request_envelope_is_checked_before_routing(system, git_repo):
    settings, _, adapters, router, dispatcher = system
    settings.router.jev.model = "m" * 15000
    paid = respx.post(URL)
    done = await run(dispatcher, request(git_repo, executor=None))
    assert_never_run(done, adapters, "compression_limit")
    assert not paid.called and router.calls == 0


@respx.mock
async def test_http_deadline_cancels_inflight_and_persists_unknown_charge(system, git_repo):
    settings, storage, adapters, _, dispatcher = system
    settings.router.jev.timeout_seconds = 0.05
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(req):
        # respx records a call only when the side effect settles with a
        # response or a normal Exception; a cancelled request never does.
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    respx.post(URL).mock(side_effect=blocked)
    done = await run(dispatcher, request(git_repo))
    metrics = assert_never_run(done, adapters, "compression_deadline")
    assert entered.is_set() and cancelled.is_set()
    assert metrics["attempted_steps"] == 1 and metrics["usage"]["cost_complete"] is False
    assert (await storage.compression_steps(done.task_id))[0]["status"] == "deadline"


@pytest.mark.parametrize("status", ["answered", "unknown"])
@respx.mock
async def test_automatic_perimeter_disabled_keeps_existing_lineage_applicability(
    system, git_repo, status
):
    _, storage, adapters, _, dispatcher = system
    ancestor = make_task(task_id="ancestor", status="completed", assigned=True)
    ancestor.decision.meta["perimeter"] = status
    await storage.insert_task(ancestor)
    paid = respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(0)))
    done = await run(dispatcher, request(git_repo, parent_task_id=ancestor.task_id))
    assert done.status == "completed", done.result
    assert paid.call_count == 2 and adapters["omp"].calls


@respx.mock
async def test_supervision_baseline_failure_does_not_claim_adapter_started(
    system, git_repo, monkeypatch
):
    from agent_dispatch.dispatch import supervision

    settings, _, adapters, _, dispatcher = system
    settings.execution.supervision.enabled = True

    def fail_baseline(*args, **kwargs):
        raise OSError("baseline unavailable")

    monkeypatch.setattr(supervision, "Evidence", fail_baseline)
    respx.post(URL).mock(return_value=httpx.Response(200, json=envelope(0)))
    done = await run(dispatcher, request(git_repo, allow_escalation=False))
    assert done.status == "failed"
    assert not any(adapter.calls for adapter in adapters.values())
    assert done.result.meta["executor_started"] is False
    with pytest.raises(FollowupError) as error:
        await dispatcher.followup(done.task_id, FollowupRequest(message="continue"))
    assert error.value.code == "no_executor_run"
