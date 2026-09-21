import json

import httpx
import pytest
import respx

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest
from agent_dispatch.routing.base import RouterError
from agent_dispatch.routing.jev import JevRouter

RESPONSE_OK = {
    "model": "typesafe/jev-1.13",
    "answers": {
        "executor": {
            "type": "choice",
            "choice": "codex",
            "probabilities": {"codex": 1},
            "confidence": 0.99,
        }
    },
    "usage": {"cost": 0.001},
    "id": "test-id",
}


def settings():
    return Settings.model_validate(
        {"secrets": {"OPENROUTER_API_KEY": "k"}, "executors": {"codex": {"adapter": "codex"}}}
    )


@pytest.mark.asyncio
@respx.mock
async def test_jev_ok():
    s = settings()
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, json=RESPONSE_OK)
    )
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(
            DispatchRequest(task="x", cwd="."), {"codex": "d"}
        )
    assert route.called and result.executor == "codex" and result.meta["jev_id"]


def fixture(name):
    return json.loads((__import__("pathlib").Path("tests/fixtures/jev") / name).read_text())


@pytest.mark.asyncio
@respx.mock
async def test_request_body_and_headers():
    s = settings()
    expected = fixture("request_executor.json")
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, json=RESPONSE_OK)
    )
    async with httpx.AsyncClient() as client:
        await JevRouter(s, client).decide(
            DispatchRequest(
                task=expected["state"]["task"],
                cwd="/tmp",
                context=expected["state"]["context"],
                files=expected["state"]["files"],
                constraints=expected["state"]["constraints"],
                source_agent="codex",
            ),
            expected["questions"]["executor"]["criteria"],
        )
    body = json.loads(route.calls[0].request.content)
    assert body["state"] == expected["state"]
    assert body["questions"]["executor"]["type"] == expected["questions"]["executor"]["type"]
    assert (
        body["questions"]["executor"]["instructions"]
        == expected["questions"]["executor"]["instructions"]
    )
    assert (
        body["questions"]["executor"]["criteria"] == expected["questions"]["executor"]["criteria"]
    )
    assert body["questions"]["difficulty"] == expected["questions"]["difficulty"]
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


def test_questions_contract():
    from agent_dispatch.routing.questions import build_questions

    questions = build_questions({"codex": "d"})
    assert all(q.get("instructions") for q in questions.values())
    assert all(
        all(isinstance(x, dict) and {"label", "description"} <= x.keys() for x in q["criteria"])
        for q in questions.values()
        if q["type"] == "score"
    )
    assert isinstance(questions["task_type"]["criteria"], dict) and all(
        isinstance(v, str) for v in questions["task_type"]["criteria"].values()
    )


@pytest.mark.asyncio
@respx.mock
async def test_full_judgments_and_metadata():
    s = settings()
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, json=fixture("response_ok_full.json"))
    )
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(
            DispatchRequest(task="x", cwd="."), {"codex": "d"}
        )
    assert route.called and set(result.judgments) == {
        "executor",
        "difficulty",
        "task_type",
        "risk",
        "ambiguity",
        "decomposable",
    }
    assert (
        result.executor == "codex"
        and result.router == "jev"
        and result.judgments["difficulty"].kind == "score"
    )
    assert result.judgments["decomposable"].value is False and result.judgments[
        "decomposable"
    ].confidence == pytest.approx(0.44)
    assert result.judgments["decomposable"].probabilities == {"true": 0.28, "false": 0.72}
    assert (
        result.meta["jev_id"]
        and result.cost_usd == pytest.approx(3.57e-5)
        and result.latency_ms >= 0
    )


@pytest.mark.asyncio
@respx.mock
async def test_invalid_json_is_router_error_without_retry():
    s = settings()
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, text="not json")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="invalid JSON"):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})
    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_server_error_retries_with_backoff():
    s = settings()
    sleeps = []
    route = respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(500, text="bad"))

    async def record(delay):
        sleeps.append(delay)

    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError):
            await JevRouter(s, client, record).decide(
                DispatchRequest(task="x", cwd="."), {"codex": "d"}
            )
    assert len(route.calls) == 3 and sleeps == [0.5, 1.0]


@pytest.mark.parametrize("status", [400, 403])
@pytest.mark.asyncio
@respx.mock
async def test_client_and_auth_errors_do_not_retry(status):
    s = settings()
    route = respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(status, text="bad"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})
    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_unknown_choice_sum_and_missing_key_errors():
    s = settings()
    for payload in [
        {"answers": {"executor": {"choice": "bad", "probabilities": {"bad": 1}}}},
        {"answers": {"executor": {"choice": "codex", "probabilities": {"codex": 0.2}}}},
        {"answers": {"executor": {"choice": "codex", "probabilities": "oops"}}},
    ]:
        route = respx.post(s.router.jev.base_url).mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(RouterError):
                await JevRouter(s, client).decide(
                    DispatchRequest(task="x", cwd="."), {"codex": "d"}
                )
        assert len(route.calls) == 1
        respx.reset()


@pytest.mark.asyncio
@respx.mock
async def test_missing_api_key_does_not_call_network():
    s = Settings.model_validate({"executors": {"codex": {"adapter": "codex"}}})
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, json=RESPONSE_OK)
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), {"codex": "d"})
    assert not route.called
