import pytest

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest, GuardReason, SourceAgent
from agent_dispatch.routing.guards import candidates, post_guards, pre_guards


def settings():
    return Settings.model_validate(
        {
            "routing": {"fallback_executor": "omp"},
            "executors": {
                "claude": {"adapter": "claude", "enabled": True},
                "omp": {"adapter": "omp", "model": "agent-lb/luna", "enabled": True},
                "omp/kimi": {"adapter": "omp", "model": "kimi", "enabled": True},
            },
        }
    )


def test_pre_guards_and_candidates(git_repo):
    s = settings()
    req = DispatchRequest(task="x", cwd=str(git_repo), source_agent=SourceAgent.omp)
    assert pre_guards(req, s, set(), True, 0) is None
    assert candidates(s, set(), SourceAgent.omp, 0) == ["claude"]
    assert (
        pre_guards(req.model_copy(update={"hop": 2}), s, set(), True, 0).reason
        == GuardReason.max_hops
    )


def test_override_and_post_guard(git_repo):
    s = settings()
    req = DispatchRequest(task="x", cwd=str(git_repo), executor="omp", max_capability="fast")
    assert pre_guards(req, s, set(), True, 0).executor == "omp"
    d = __import__("agent_dispatch.models", fromlist=["RouteDecision"]).RouteDecision(
        router="jev", executor="claude", confidence=0.5, scores={"claude": 0.5, "omp": 0.5}
    )
    out, events = post_guards(d, s, ["claude", "omp"])
    assert out.reason == GuardReason.low_confidence and events


@pytest.mark.parametrize("kind", ["missing", "file", "not_git"])
def test_pre_bad_cwd_variants(tmp_path, kind):
    cwd = tmp_path / kind
    if kind == "file":
        cwd.write_text("x")
    elif kind == "not_git":
        cwd.mkdir()
    event = pre_guards(DispatchRequest(task="x", cwd=str(cwd)), settings(), set(), True, 0)
    assert event.reason == GuardReason.bad_cwd


def test_hop_boundary(git_repo):
    s = settings()
    req = DispatchRequest(task="x", cwd=str(git_repo), hop=1)
    assert pre_guards(req, s, set(), True, 0) is None
    assert (
        pre_guards(req.model_copy(update={"hop": 2}), s, set(), True, 0).reason
        == GuardReason.max_hops
    )


def test_unknown_parent(git_repo):
    e = pre_guards(
        DispatchRequest(task="x", cwd=str(git_repo), parent_task_id="p"),
        settings(),
        set(),
        False,
        0,
    )
    assert e.reason == GuardReason.unknown_parent


def test_max_children_boundary(git_repo):
    r = DispatchRequest(task="x", cwd=str(git_repo), parent_task_id="p")
    assert pre_guards(r, settings(), set(), True, 1) is None
    assert pre_guards(r, settings(), set(), True, 2).reason == GuardReason.max_children
    assert (
        pre_guards(r.model_copy(update={"parent_task_id": None}), settings(), set(), True, 99)
        is None
    )


def test_max_children_refusal_names_running_siblings(git_repo):
    r = DispatchRequest(task="x", cwd=str(git_repo), parent_task_id="p")
    quiet = pre_guards(r, settings(), set(), True, 2)
    assert quiet.reason == GuardReason.max_children
    assert "status(task_id, wait_seconds)" not in quiet.detail
    event = pre_guards(r, settings(), set(), True, 2, running_siblings=["abc123", "def456"])
    assert event.reason == GuardReason.max_children
    assert "running siblings: abc123, def456" in event.detail
    assert "status(task_id, wait_seconds)" in event.detail
    assert "instead of dispatching again" in event.detail
    long = pre_guards(r, settings(), set(), True, 2, running_siblings=[f"t{i}" for i in range(7)])
    assert "t0, t1, t2, t3, t4, t5 (+1 more)" in long.detail
    assert "t6" not in long.detail


def test_override_disabled(git_repo):
    s = settings()
    s.executors["claude"].enabled = False
    assert (
        pre_guards(
            DispatchRequest(task="x", cwd=str(git_repo), executor="claude"), s, set(), True, 0
        ).reason
        == GuardReason.disabled
    )


def test_override_unavailable(git_repo):
    assert (
        pre_guards(
            DispatchRequest(task="x", cwd=str(git_repo), executor="claude"),
            settings(),
            {"claude"},
            True,
            0,
        ).reason
        == GuardReason.unavailable
    )


def test_override_ok_same_source(git_repo):
    r = DispatchRequest(
        task="x", cwd=str(git_repo), executor="claude", source_agent=SourceAgent.claude
    )
    assert pre_guards(r, settings(), set(), True, 0).router == "override"


def test_candidates_agent_source_excludes_only_its_pool():
    assert candidates(settings(), set(), SourceAgent.omp, 0) == ["claude"]
    # Источник codex остаётся валидным и не исключает omp-пул.
    assert candidates(settings(), set(), SourceAgent.codex, 0) == ["claude", "omp", "omp/kimi"]


def test_candidates_hop_one_and_flag_false_keep_source():
    assert len(candidates(settings(), set(), SourceAgent.opencode, 1)) == 3
    s = settings()
    s.routing.exclude_source_agent = False
    assert len(candidates(s, set(), SourceAgent.opencode, 0)) == 3


def test_candidates_unavailable_disabled_subtracted():
    s = settings()
    s.executors["claude"].enabled = False
    assert candidates(s, {"omp"}, SourceAgent.unknown, 0) == ["omp/kimi"]


@pytest.mark.parametrize("source", [SourceAgent.cli, SourceAgent.unknown])
def test_candidates_cli_unknown_keep_all(source):
    assert len(candidates(settings(), set(), source, 0)) == 3


def test_post_low_confidence():
    from agent_dispatch.models import RouteDecision, RouterKind

    out, events = post_guards(
        RouteDecision(
            router=RouterKind.jev,
            executor="claude",
            confidence=0.5,
            scores={"claude": 0.9, "omp": 0.1},
        ),
        settings(),
        ["claude", "omp"],
    )
    assert (
        out.reason == GuardReason.low_confidence and events[0].reason == GuardReason.low_confidence
    )


def test_post_margin_boundary_passes():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev,
        executor="claude",
        confidence=1,
        scores={"claude": 0.55, "omp": 0.45},
    )
    assert not post_guards(d, settings(), ["claude", "omp"])[1]


def test_post_margin_below_fails():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev,
        executor="claude",
        confidence=1,
        scores={"claude": 0.54, "omp": 0.46},
    )
    assert post_guards(d, settings(), ["claude", "omp"])[0].reason == GuardReason.low_margin


def test_post_fallback_not_candidate_warning():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=0.5, scores={"claude": 0.9}
    )
    out, events = post_guards(d, settings(), ["claude"])
    assert (
        out.executor == "claude"
        and "warning" in out.meta
        and events[-1].reason == GuardReason.unavailable
    )


def test_post_ok_no_mutation_or_events():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=1, scores={"claude": 0.9, "omp": 0.1}
    )
    out, events = post_guards(d, settings(), ["claude", "omp"])
    assert out is not d and not events
    assert out.executor == d.executor and out.scores == d.scores
    # Уверенный выбор помечается как autonomous, но не подменяется.
    assert out.confidence_tier == "autonomous"
    assert out.model_copy(update={"confidence_tier": None}) == d


def chain_settings(chain):
    return Settings.model_validate(
        {
            "routing": {"fallback_executor": chain},
            "executors": {
                "claude": {"adapter": "claude"},
                "omp": {"adapter": "omp", "model": "agent-lb/luna"},
                "omp/kimi": {"adapter": "omp", "model": "kimi"},
            },
        }
    )


def test_post_fallback_chain_takes_first_available_member():
    """Первый в цепочке остывает: слабое решение уходит к следующему, а не к top1."""
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=0.5, scores={"claude": 0.9}
    )
    out, events = post_guards(d, chain_settings(["omp/kimi", "omp"]), ["claude", "omp"])
    assert out.executor == "omp"
    assert "warning" not in out.meta
    assert [e.reason for e in events] == [GuardReason.low_confidence]


def test_post_fallback_chain_prefers_head_when_available():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=0.5, scores={"claude": 0.9}
    )
    candidates_ = ["claude", "omp", "omp/kimi"]
    out, _ = post_guards(d, chain_settings(["omp/kimi", "omp"]), candidates_)
    assert out.executor == "omp/kimi"


def test_post_fallback_chain_exhausted_keeps_top1_with_warning():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=0.5, scores={"claude": 0.9}
    )
    out, events = post_guards(d, chain_settings(["omp/kimi", "omp"]), ["claude"])
    assert out.executor == "claude"
    assert out.meta["warning"] == "fallback executor unavailable: omp/kimi, omp"
    assert events[-1].reason == GuardReason.unavailable


def perimeter_settings(chain):
    return Settings.model_validate(
        {
            "routing": {"fallback_executor": chain},
            "executors": {
                "internal": {"adapter": "omp", "model": "m", "corporate": True},
                "omp": {"adapter": "omp", "model": "agent-lb/luna"},
                "claude": {"adapter": "claude"},
            },
        }
    )


def _corporate_judgment():
    from agent_dispatch.models import Judgment

    return Judgment(kind="noul", value=True, confidence=0.9, probabilities={})


def test_post_low_confidence_keeps_fallback_inside_perimeter():
    """Слабое решение, суженное до периметра: fallback не выводит задачу наружу."""
    from agent_dispatch.models import RouteDecision, RouterKind

    s = perimeter_settings(["omp"])
    d = RouteDecision(
        router=RouterKind.jev,
        executor="internal",
        confidence=0.3,
        scores={"internal": 0.9},
        judgments={"corporate_data": _corporate_judgment()},
    )
    out, events = post_guards(d, s, ["internal", "omp"])
    assert out.executor == "internal"
    assert out.meta["perimeter"] == "answered"
    # Цепочка без исполнителя периметра: задача остаётся у внутреннего
    # исполнителя, событие unavailable объясняет, почему fallback не взят.
    assert [e.reason for e in events] == [GuardReason.low_confidence, GuardReason.unavailable]
    assert "corporate perimeter" in out.meta["warning"]


def test_post_guards_narrows_unanswered_perimeter_to_corporate():
    """claude_local вопрос не задаёт: уверенный внешний выбор сужается (fail closed)."""
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.claude_local, executor="claude", confidence=0.99, scores={"claude": 0.99}
    )
    out, _ = post_guards(d, perimeter_settings(["omp"]), ["internal", "omp", "claude"])
    assert out.executor == "internal"
    assert out.meta["perimeter"] == "unknown"
    assert any("fail closed" in note for note in out.meta["selection_notes"])


def test_post_weak_without_corporate_in_chain_stays_inside_perimeter():
    """В цепочке нет исполнителя периметра: выбор остаётся в пуле, с предупреждением."""
    from agent_dispatch.models import RouteDecision, RouterKind

    s = perimeter_settings(["omp"])
    d = RouteDecision(
        router=RouterKind.jev,
        executor="claude",
        confidence=0.3,
        scores={"claude": 0.9, "internal": 0.1},
        judgments={"corporate_data": _corporate_judgment()},
    )
    out, events = post_guards(d, s, ["internal", "claude", "omp"])
    assert out.executor == "internal"
    assert "corporate perimeter" in out.meta["warning"]
    assert events[-1].reason == GuardReason.unavailable


def test_post_fallback_respects_max_capability():
    """Слабое решение не уходит к fallback выше потолка запроса."""
    from agent_dispatch.models import RouteDecision, RouterKind

    s = Settings.model_validate(
        {
            "routing": {"fallback_executor": ["strong"]},
            "executors": {
                "cheap": {"adapter": "omp", "model": "agent-lb/luna", "tier": "fast"},
                "strong": {"adapter": "claude", "tier": "strong"},
            },
        }
    )
    # top1 по оценкам роутера выше потолка: подмена на него нарушила бы потолок.
    d = RouteDecision(
        router=RouterKind.jev,
        executor="cheap",
        confidence=0.3,
        scores={"strong": 0.95, "cheap": 0.9},
    )
    out, _ = post_guards(d, s, ["cheap", "strong"], max_capability="fast")
    assert out.executor == "cheap"
    # Цепочка содержит только исполнителя выше потолка: выбор роутера остаётся.
    assert out.meta["warning"] == "fallback executor unavailable: strong"


def test_post_fallback_keeps_perimeter_over_ceiling():
    """Периметр сильнее потолка: fallback выше `max_capability` остаётся внутри периметра."""
    from agent_dispatch.models import RouteDecision, RouterKind

    s = Settings.model_validate(
        {
            "routing": {"fallback_executor": ["external"]},
            "executors": {
                "strong_internal": {
                    "adapter": "omp",
                    "model": "m",
                    "tier": "strong",
                    "corporate": True,
                },
                "external": {"adapter": "omp", "model": "agent-lb/luna", "tier": "fast"},
            },
        }
    )
    d = RouteDecision(
        router=RouterKind.jev,
        executor="strong_internal",
        confidence=0.3,
        scores={"strong_internal": 0.9},
        judgments={"corporate_data": _corporate_judgment()},
    )
    out, _ = post_guards(d, s, ["strong_internal", "external"], max_capability="fast")
    assert out.executor == "strong_internal"
    assert any("perimeter was kept" in note for note in out.meta["selection_notes"])


def test_post_guards_keeps_perimeter_when_all_corporate_executors_are_cooling():
    """Остывание всех корпоративных исполнителей не открывает периметр молча."""
    from agent_dispatch.models import Judgment, RouteDecision, RouterKind

    s = Settings.model_validate(
        {
            "routing": {"fallback_executor": ["external"]},
            "executors": {
                "external": {"adapter": "omp", "model": "agent-lb/luna", "tier": "balanced"},
                "internal": {
                    "adapter": "omp",
                    "model": "m",
                    "tier": "balanced",
                    "corporate": True,
                },
            },
        }
    )
    d = RouteDecision(
        router=RouterKind.jev,
        executor="external",
        confidence=0.99,
        scores={"external": 0.99},
        judgments={
            "corporate_data": Judgment(
                kind="noul", value=True, confidence=0.9, probabilities={"true": 0.95}
            )
        },
    )
    # `internal` остывает и в кандидаты не попал.
    out, _ = post_guards(d, s, ["external"])
    assert out.meta["perimeter"] == "answered"
    assert any("no in-perimeter executor" in note for note in out.meta["selection_notes"])


def omp_settings():
    return Settings.model_validate(
        {
            "routing": {"fallback_executor": "omp/sol", "review_only": {"omp": ["claude"]}},
            "executors": {
                "claude": {"adapter": "claude"},
                "omp/sol": {"adapter": "omp", "model": "agent-lb/gpt-6.1-sol"},
                "omp/x5-code-large": {
                    "adapter": "omp",
                    "model": "copilot-x5/x5-airun-code-large",
                    "corporate": True,
                },
            },
        }
    )


def test_candidates_exclude_omp_adapter_for_omp_source(git_repo):
    # Источник omp не получает работу в свой же адаптер, как codex и opencode.
    s = omp_settings()
    assert candidates(s, set(), SourceAgent.omp, 0) == ["claude"]
    assert len(candidates(s, set(), SourceAgent.omp, 1)) == 3


def test_perimeter_narrows_the_choice_to_omp_x5():
    """Корпоративные данные: исполнитель берётся только из omp/x5-*."""
    from agent_dispatch.models import RouteDecision, RouterKind

    s = omp_settings()
    d = RouteDecision(
        router=RouterKind.jev,
        executor="omp/sol",
        confidence=0.99,
        scores={"omp/sol": 0.99},
        judgments={"corporate_data": _corporate_judgment()},
    )
    out, _ = post_guards(d, s, ["claude", "omp/sol", "omp/x5-code-large"])
    assert out.executor == "omp/x5-code-large"
    assert out.meta["perimeter"] == "answered"


def test_review_only_for_omp_source_bars_claude_from_edits(git_repo):
    from agent_dispatch.routing.guards import review_only_executors

    s = omp_settings()
    req = DispatchRequest(task="x", cwd=str(git_repo), source_agent=SourceAgent.omp)
    assert review_only_executors(req, s) == {"claude"}
    review = req.model_copy(update={"kind": "review"})
    assert review_only_executors(review, s) == set()
