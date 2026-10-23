import json
from pathlib import Path

import httpx
import pytest
import respx

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.models import DispatchRequest
from agent_dispatch.routing.base import RouterError
from agent_dispatch.routing.jev import JevRouter

RESPONSE_OK = {
    "model": "typesafe/jev-1.13",
    "answers": {
        "capability": {
            "type": "choice",
            "choice": "balanced",
            "probabilities": {"balanced": 1},
            "confidence": 0.99,
        }
    },
    "usage": {"cost": 0.001},
    "id": "test-id",
}

CANDIDATES = {"codex": ExecutorSettings(adapter="codex", tier="balanced")}


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
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
    assert route.called and result.executor == "codex" and result.meta["jev_id"]
    assert result.capability == "balanced"


def fixture(name):
    return json.loads((Path("tests/fixtures/jev") / name).read_text())


@pytest.mark.asyncio
@respx.mock
async def test_request_body_and_headers():
    s = settings()
    expected = fixture("request_capability.json")
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
            CANDIDATES,
        )
    body = json.loads(route.calls[0].request.content)
    assert body["state"] == expected["state"]
    assert body["questions"]["capability"] == expected["questions"]["capability"]
    assert body["questions"]["difficulty"] == expected["questions"]["difficulty"]
    assert body["questions"]["judgment"] == expected["questions"]["judgment"]
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


def test_questions_contract():
    from agent_dispatch.routing.questions import build_questions

    questions = build_questions(["fast", "balanced"], ask_corporate=True)
    assert all(q.get("instructions") for q in questions.values())
    assert all(
        all(isinstance(x, dict) and {"label", "description"} <= x.keys() for x in q["criteria"])
        for q in questions.values()
        if q["type"] == "score"
    )
    assert isinstance(questions["task_type"]["criteria"], dict) and all(
        isinstance(v, str) for v in questions["task_type"]["criteria"].values()
    )
    # Тиры без кандидатов Jev не предлагаются, иначе он выберет то, чего нет.
    assert set(questions["capability"]["criteria"]) == {"fast", "balanced"}
    assert "corporate_data" in questions


def test_corporate_question_is_asked_only_when_it_can_change_the_outcome():
    from agent_dispatch.routing.questions import build_questions

    assert "corporate_data" not in build_questions(["balanced"])


@pytest.mark.asyncio
@respx.mock
async def test_full_judgments_and_metadata():
    s = settings()
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, json=fixture("response_ok_full.json"))
    )
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
    assert route.called and set(result.judgments) == {
        "capability",
        "judgment",
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
    # noul разбирается по самой фикстуре: 0.5 это «не знаю», а не «средне».
    p = fixture("response_ok_full.json")["answers"]["decomposable"]["noul"]
    decomposable = result.judgments["decomposable"]
    assert decomposable.value is (p >= 0.5)
    assert decomposable.confidence == pytest.approx(abs(p - 0.5) * 2)
    assert decomposable.probabilities == pytest.approx({"true": p, "false": 1 - p})
    assert (
        result.meta["jev_id"]
        and result.cost_usd == pytest.approx(3.6624e-5)
        and result.latency_ms >= 0
    )


@pytest.mark.asyncio
@respx.mock
async def test_capability_maps_to_the_executor_of_that_tier():
    s = settings()
    candidates = {
        "cheap": ExecutorSettings(adapter="opencode", model="m", tier="fast"),
        "codex": ExecutorSettings(adapter="codex", tier="balanced"),
        "opus": ExecutorSettings(adapter="claude", tier="strong"),
    }
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "fast",
                "probabilities": {"fast": 0.9, "balanced": 0.08, "strong": 0.02},
                "confidence": 0.9,
            }
        }
    }
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)
    assert result.executor == "cheap"
    # Вероятности тиров переносятся на их представителей, чтобы margin считался как раньше.
    assert result.scores["cheap"] == pytest.approx(0.9)
    assert result.scores["opus"] == pytest.approx(0.02)


@pytest.mark.asyncio
@respx.mock
async def test_corporate_answer_keeps_the_task_inside_the_perimeter():
    s = settings()
    candidates = {
        "external": ExecutorSettings(adapter="codex", tier="fast"),
        "internal": ExecutorSettings(adapter="opencode", model="m", tier="fast", corporate=True),
    }
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "fast",
                "probabilities": {"fast": 1},
                "confidence": 0.95,
            },
            "corporate_data": {"type": "noul", "noul": 0.89},
        }
    }
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)
    assert result.executor == "internal"
    assert any("corporate" in note for note in result.meta["selection_notes"])


@pytest.mark.asyncio
@respx.mock
async def test_unsure_corporate_answer_below_the_threshold_does_not_narrow_the_perimeter():
    # noul 0.53 это уверенность 0.06 — монетка. С поднятым порогом такой ответ
    # не должен вслепую отсекать весь пул до периметра.
    s = Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "k"},
            "executors": {"codex": {"adapter": "codex"}},
            "routing": {"corporate_min_confidence": 0.5},
        }
    )
    candidates = {
        "external": ExecutorSettings(adapter="claude", tier="strong"),
        "internal": ExecutorSettings(adapter="opencode", model="m", tier="fast", corporate=True),
    }
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "strong",
                "probabilities": {"strong": 1},
                "confidence": 0.95,
            },
            "corporate_data": {"type": "noul", "noul": 0.53},
        }
    }
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)

    assert result.executor == "external"
    assert any("perimeter filter was not applied" in n for n in result.meta["selection_notes"])


@pytest.mark.asyncio
@respx.mock
async def test_confident_corporate_answer_still_narrows_the_perimeter_at_the_same_threshold():
    s = Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "k"},
            "executors": {"codex": {"adapter": "codex"}},
            "routing": {"corporate_min_confidence": 0.5},
        }
    )
    candidates = {
        "external": ExecutorSettings(adapter="claude", tier="strong"),
        "internal": ExecutorSettings(adapter="opencode", model="m", tier="fast", corporate=True),
    }
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "strong",
                "probabilities": {"strong": 1},
                "confidence": 0.95,
            },
            "corporate_data": {"type": "noul", "noul": 0.95},
        }
    }
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)

    assert result.executor == "internal"
    assert any("requested level 'strong'" in n for n in result.meta["selection_notes"])


@pytest.mark.asyncio
@respx.mock
async def test_corporate_perimeter_off_does_not_ask_the_question():
    s = Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "k"},
            "executors": {"codex": {"adapter": "codex"}},
            "routing": {"corporate_perimeter": False},
        }
    )
    candidates = {
        "external": ExecutorSettings(adapter="codex", tier="fast"),
        "internal": ExecutorSettings(adapter="opencode", model="m", tier="fast", corporate=True),
    }
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "answers": {
                    "capability": {
                        "type": "choice",
                        "choice": "fast",
                        "probabilities": {"fast": 1},
                        "confidence": 0.9,
                    }
                }
            },
        )
    )
    async with httpx.AsyncClient() as client:
        await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)
    body = json.loads(route.calls[0].request.content)
    assert "corporate_data" not in body["questions"]


@pytest.mark.asyncio
@respx.mock
async def test_corporate_perimeter_off_ignores_the_answer():
    # Даже если Jev почему-то отвечает «корп. данные: да» уверенно, при
    # выключенном периметре пул не сужается и заметки о периметре нет.
    s = Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "k"},
            "executors": {"codex": {"adapter": "codex"}},
            "routing": {"corporate_perimeter": False},
        }
    )
    candidates = {
        "external": ExecutorSettings(adapter="claude", tier="strong"),
        "internal": ExecutorSettings(adapter="opencode", model="m", tier="fast", corporate=True),
    }
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "strong",
                "probabilities": {"strong": 1},
                "confidence": 0.95,
            },
            "corporate_data": {"type": "noul", "noul": 0.95},
        }
    }
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)

    assert result.executor == "external"
    assert not any("corporate" in n for n in result.meta.get("selection_notes", []))


@pytest.mark.asyncio
@respx.mock
async def test_judgment_answer_sends_the_task_to_a_reasoning_agent():
    s = settings()
    candidates = {
        "codex": ExecutorSettings(adapter="codex", tier="strong"),
        "claude": ExecutorSettings(adapter="claude", tier="strong"),
    }
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "strong",
                "probabilities": {"strong": 1},
                "confidence": 0.97,
            },
            "judgment": {"type": "noul", "noul": 0.83},
        }
    }
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)
    assert result.executor == "claude"


@pytest.mark.asyncio
@respx.mock
async def test_invalid_json_is_router_error_without_retry():
    s = settings()
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, text="not json")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="invalid JSON"):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
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
                DispatchRequest(task="x", cwd="."), CANDIDATES
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
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "answer",
    [
        {"type": "noul"},
        {"type": "noul", "noul": float("nan")},
        {"type": "noul", "noul": 1.5},
        {"type": "noul", "noul": -0.1},
    ],
)
async def test_noul_without_valid_probability_is_router_error(answer):
    # Молчаливый или вне [0, 1] ответ читался бы как уверенное «да»/«нет».
    s = settings()
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "balanced",
                "probabilities": {"balanced": 1},
                "confidence": 0.95,
            },
            "judgment": answer,
        }
    }
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, content=json.dumps(payload))
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="invalid noul answer: judgment"):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_score_probability_out_of_range_is_router_error():
    s = settings()
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "balanced",
                "probabilities": {"balanced": 1},
                "confidence": 0.95,
            },
            "difficulty": {"type": "score", "score": 1, "probabilities": {"1": 1.5}},
        }
    }
    route = respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="invalid probabilities"):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_missing_corporate_answer_is_router_error():
    # Вопрос задан, ответа нет: неполный ответ молча читался бы как уверенное
    # «нет» и открывал периметр. Это ошибка роутера, дальше работает fallback
    # цепочка, где периметр всё равно применяется (fail closed).
    s = settings()
    candidates = {
        "external": ExecutorSettings(adapter="codex", tier="fast"),
        "internal": ExecutorSettings(adapter="opencode", model="m", tier="fast", corporate=True),
    }
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "fast",
                "probabilities": {"fast": 1},
                "confidence": 0.95,
            }
        }
    }
    route = respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="missing answer: corporate_data"):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)
    assert len(route.calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_unknown_choice_sum_and_missing_key_errors():
    s = settings()
    for payload in [
        {"answers": {"capability": {"choice": "bad", "probabilities": {"bad": 1}}}},
        {"answers": {"capability": {"choice": "balanced", "probabilities": {"balanced": 0.2}}}},
        {"answers": {"capability": {"choice": "balanced", "probabilities": "oops"}}},
    ]:
        route = respx.post(s.router.jev.base_url).mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(RouterError):
                await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
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
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), CANDIDATES)
    assert not route.called


def _corporate_payload(corporate_answer, *, confidence=0.95):
    return {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "fast",
                "probabilities": {"fast": 1},
                "confidence": confidence,
            },
            "corporate_data": corporate_answer,
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_corporate_answer_of_wrong_type_is_router_error():
    # `score` со значением 0 читался бы как уверенное «нет» и открывал периметр.
    s = settings()
    candidates = {
        "external": ExecutorSettings(adapter="codex", tier="fast"),
        "internal": ExecutorSettings(adapter="opencode", model="m", tier="fast", corporate=True),
    }
    payload = _corporate_payload(
        {"type": "score", "score": 0, "confidence": 1, "probabilities": {"0": 1}}
    )
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="invalid noul answer: corporate_data"):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)


@pytest.mark.asyncio
@respx.mock
async def test_nan_capability_confidence_is_router_error():
    # NaN прошёл бы мимо порога low_confidence: сравнение с NaN всегда ложно.
    s = settings()
    candidates = {"codex": ExecutorSettings(adapter="codex", tier="fast")}
    payload = {
        "answers": {
            "capability": {
                "type": "choice",
                "choice": "fast",
                "probabilities": {"fast": 1},
                "confidence": "NaN",
            }
        }
    }
    respx.post(s.router.jev.base_url).mock(return_value=httpx.Response(200, json=payload))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError):
            await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)


@pytest.mark.asyncio
@respx.mock
async def test_corporate_question_is_asked_when_corporate_executors_are_cooling():
    # Корпоративный исполнитель остывает и в кандидатах его нет, но в конфиге
    # он есть: вопрос задаётся, иначе периметр открывался бы молча.
    s = Settings.model_validate(
        {
            "secrets": {"OPENROUTER_API_KEY": "k"},
            "routing": {"fallback_executor": ["external"]},
            "executors": {
                "external": {"adapter": "codex", "tier": "fast"},
                "internal": {
                    "adapter": "opencode",
                    "model": "m",
                    "tier": "fast",
                    "corporate": True,
                },
            },
        }
    )
    candidates = {"external": s.executors["external"]}
    route = respx.post(s.router.jev.base_url).mock(
        return_value=httpx.Response(200, json=_corporate_payload({"type": "noul", "noul": 0.9}))
    )
    async with httpx.AsyncClient() as client:
        result = await JevRouter(s, client).decide(DispatchRequest(task="x", cwd="."), candidates)
    sent = json.loads(route.calls[0].request.content)
    assert "corporate_data" in sent["questions"]
    assert result.meta["perimeter"] == "answered"


def test_probability_rounding_noise_is_clamped_not_an_error():
    from agent_dispatch.routing.questions import parse_answers

    _, confidence, _, judgments = parse_answers(
        {
            "capability": {
                "type": "choice",
                "choice": "fast",
                "probabilities": {"fast": 1.0000001},
                "confidence": 1.0000001,
            },
            "corporate_data": {"type": "noul", "noul": 1.0000001},
        },
        ["fast"],
    )
    assert confidence == 1.0
    assert judgments["corporate_data"].probabilities["true"] == 1.0
