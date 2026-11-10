from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest, FollowupRequest, TaskStatus
from agent_dispatch.telemetry.storage import Storage
from tests.test_review_fix_round import make_dispatcher, request, result


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", [None, "first", "second"])
async def test_stages_retry_only_the_failed_stage(tmp_path, git_repo, failed_stage):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path, reviewers=["first", "second"])
    order = []
    for name in ("first", "second"):

        async def review(ctx, name=name):
            order.append(name)
            verdict = "fail" if name == failed_stage and len(adapters[name].calls) == 1 else "pass"
            adapters[name].result = result(name, changed_files=[], verdict=verdict)

        adapters[name].on_execute = review
    task = await dispatcher.submit(request(git_repo))
    done = await dispatcher.wait(task.task_id, 10)
    expected = (
        ["first", "second"]
        if failed_stage is None
        else (
            ["first", "first", "second"]
            if failed_stage == "first"
            else ["first", "second", "second"]
        )
    )
    assert order == expected
    assert done.status == TaskStatus.completed
    assert done.result.meta["review"]["by"] == "second"
    assert [stage["by"] for stage in done.result.meta["review_stages"]] == expected
    assert [stage["round"] for stage in done.result.meta["review_stages"]] == (
        [0, 0] if failed_stage is None else [0, 1, 1] if failed_stage == "first" else [0, 0, 1]
    )
    assert await storage.count_children(task.task_id) == 0
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_fails", [False, True])
async def test_shared_round_budget_stops_before_senior_review(tmp_path, git_repo, second_fails):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path, reviewers=["first", "second"])
    adapters["first"].result = result("first", changed_files=[], verdict="fail")
    if second_fails:

        async def fix(ctx):
            if len(adapters["worker"].calls) == 2:
                adapters["first"].result = result("first", changed_files=[], verdict="pass")

        adapters["worker"].on_execute = fix
        adapters["second"].result = result("second", changed_files=[], verdict="fail")
    task = await dispatcher.submit(request(git_repo))
    done = await dispatcher.wait(task.task_id, 10)
    original = await storage.get_task(task.task_id)
    assert done.decision.executor == "senior"
    assert len(adapters["worker"].calls) == 2
    assert len(original.result.meta["fix_rounds"]) == 1
    assert len(adapters["second"].calls) == int(second_fails)
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_outcome", ["unknown", "skipped"])
async def test_unknown_or_skipped_stage_advances_without_fix(tmp_path, git_repo, first_outcome):
    storage, adapters, dispatcher = await make_dispatcher(tmp_path, reviewers=["first", "second"])
    if first_outcome == "unknown":
        adapters["first"].result = result("first", changed_files=[], verdict=None)
    else:
        adapters["first"].raise_exc = RuntimeError("review unavailable")
    task = await dispatcher.submit(request(git_repo))
    done = await dispatcher.wait(task.task_id, 10)
    assert done.status == TaskStatus.completed
    assert len(adapters["second"].calls) == 1
    assert len(adapters["worker"].calls) == 1
    assert done.result.meta["review_stages"][0]["verdict"] == first_outcome
    assert "fix_rounds" not in done.result.meta
    await storage.close()


@pytest.mark.asyncio
async def test_auto_reviews_ignore_full_child_quota_but_user_reviews_do_not(tmp_path, git_repo):
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path, reviewers=["first", "second"], routing={"max_children": 1}
    )
    normal_child = None

    async def work(ctx):
        nonlocal normal_child
        normal_child = await dispatcher.submit(
            DispatchRequest(
                task="ordinary child",
                cwd=str(git_repo),
                kind="review",
                executor="senior",
                parent_task_id=ctx.task_id,
                hop=1,
                allow_escalation=False,
            )
        )
        await dispatcher.wait(normal_child.task_id, 5)

    adapters["worker"].on_execute = work
    task = await dispatcher.submit(request(git_repo))
    done = await dispatcher.wait(task.task_id, 10)
    assert done.status == TaskStatus.completed
    assert len(adapters["first"].calls) == len(adapters["second"].calls) == 1
    assert await storage.count_children(task.task_id) == 1
    denied = await dispatcher.submit(
        DispatchRequest(
            task="user review",
            cwd=str(git_repo),
            kind="review",
            executor="senior",
            parent_task_id=task.task_id,
            hop=1,
            allow_escalation=False,
        )
    )
    refused = await dispatcher.wait(denied.task_id, 5)
    assert refused.result.meta["guard"] == "max_children"
    auto_review = done.result.meta["review"]["task_id"]
    continuation = await dispatcher.followup(
        auto_review, FollowupRequest(message="review once more", allow_escalation=False)
    )
    refused_followup = await dispatcher.wait(continuation.task_id, 5)
    assert not refused_followup.request._automatic_review
    assert refused_followup.result.meta["guard"] == "max_children"
    # The exemption survives reloading stored requests, but is not an API input.
    await storage.close()
    restored = Storage(tmp_path / "db.sqlite")
    await restored.open()
    assert await restored.count_children(task.task_id) == 1
    saved = await restored.get_task(done.result.meta["review"]["task_id"])
    assert saved.request._automatic_review
    assert saved.request.model_copy()._automatic_review
    assert not normal_child.request._automatic_review
    with pytest.raises(ValidationError):
        DispatchRequest.model_validate(
            {"task": "spoof", "cwd": str(git_repo), "kind": "review", "_automatic_review": True}
        )
    await restored.close()


@pytest.mark.asyncio
async def test_perimeter_uses_only_its_corporate_stages(tmp_path, git_repo):
    storage, adapters, dispatcher = await make_dispatcher(
        tmp_path, corporate=True, reviewers=["first", "second"]
    )
    dispatcher.settings.executors["senior"].corporate = False
    dispatcher.settings.executors["worker"].review_by = "senior"
    task = await dispatcher.submit(request(git_repo, prod=True))
    done = await dispatcher.wait(task.task_id, 10)
    assert done.status == TaskStatus.completed
    assert [s["by"] for s in done.result.meta["review_stages"]] == ["first", "second"]
    assert not adapters["senior"].calls
    assert "senior" not in str(done.result.meta["review_stages"])
    await storage.close()


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("review_by", ["first", "first"], "duplicate"),
        ("review_by", ["first", "worker"], "itself"),
        ("review_by", ["missing"], "unknown executor"),
        ("perimeter_review_by", ["first", "second"], "corporate"),
    ],
)
@pytest.mark.asyncio
async def test_review_stage_config_rejects_invalid_members(tmp_path, field, value, message):
    storage, _, dispatcher = await make_dispatcher(tmp_path)
    data = dispatcher.settings.model_dump()
    data["executors"]["worker"][field] = value
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(data)
    await storage.close()
