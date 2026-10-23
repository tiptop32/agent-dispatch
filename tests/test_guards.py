import pytest

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest, GuardReason, SourceAgent
from agent_dispatch.routing.guards import candidates, post_guards, pre_guards


def settings():
    return Settings.model_validate(
        {
            "routing": {"fallback_executor": "codex"},
            "executors": {
                "claude": {"adapter": "claude", "enabled": True},
                "codex": {"adapter": "codex", "enabled": True},
                "opencode/kimi": {"adapter": "opencode", "model": "kimi", "enabled": True},
            },
        }
    )


def test_pre_guards_and_candidates(git_repo):
    s = settings()
    req = DispatchRequest(task="x", cwd=str(git_repo), source_agent=SourceAgent.opencode)
    assert pre_guards(req, s, set(), True, 0) is None
    assert candidates(s, set(), SourceAgent.opencode, 0) == ["claude", "codex"]
    assert (
        pre_guards(req.model_copy(update={"hop": 2}), s, set(), True, 0).reason
        == GuardReason.max_hops
    )


def test_override_and_post_guard(git_repo):
    s = settings()
    req = DispatchRequest(task="x", cwd=str(git_repo), executor="codex", max_capability="fast")
    assert pre_guards(req, s, set(), True, 0).executor == "codex"
    d = __import__("agent_dispatch.models", fromlist=["RouteDecision"]).RouteDecision(
        router="jev", executor="claude", confidence=0.5, scores={"claude": 0.5, "codex": 0.5}
    )
    out, events = post_guards(d, s, ["claude", "codex"])
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


def test_candidates_codex_excludes_only_codex():
    assert candidates(settings(), set(), SourceAgent.codex, 0) == ["claude", "opencode/kimi"]


def test_candidates_hop_one_and_flag_false_keep_source():
    assert len(candidates(settings(), set(), SourceAgent.opencode, 1)) == 3
    s = settings()
    s.routing.exclude_source_agent = False
    assert len(candidates(s, set(), SourceAgent.opencode, 0)) == 3


def test_candidates_unavailable_disabled_subtracted():
    s = settings()
    s.executors["claude"].enabled = False
    assert candidates(s, {"codex"}, SourceAgent.unknown, 0) == ["opencode/kimi"]


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
            scores={"claude": 0.9, "codex": 0.1},
        ),
        settings(),
        ["claude", "codex"],
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
        scores={"claude": 0.55, "codex": 0.45},
    )
    assert not post_guards(d, settings(), ["claude", "codex"])[1]


def test_post_margin_below_fails():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev,
        executor="claude",
        confidence=1,
        scores={"claude": 0.54, "codex": 0.46},
    )
    assert post_guards(d, settings(), ["claude", "codex"])[0].reason == GuardReason.low_margin


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
        router=RouterKind.jev, executor="claude", confidence=1, scores={"claude": 0.9, "codex": 0.1}
    )
    out, events = post_guards(d, settings(), ["claude", "codex"])
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
                "codex": {"adapter": "codex"},
                "opencode/kimi": {"adapter": "opencode", "model": "kimi"},
            },
        }
    )


def test_post_fallback_chain_takes_first_available_member():
    """Первый в цепочке остывает: слабое решение уходит к следующему, а не к top1."""
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=0.5, scores={"claude": 0.9}
    )
    out, events = post_guards(d, chain_settings(["opencode/kimi", "codex"]), ["claude", "codex"])
    assert out.executor == "codex"
    assert "warning" not in out.meta
    assert [e.reason for e in events] == [GuardReason.low_confidence]


def test_post_fallback_chain_prefers_head_when_available():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=0.5, scores={"claude": 0.9}
    )
    candidates_ = ["claude", "codex", "opencode/kimi"]
    out, _ = post_guards(d, chain_settings(["opencode/kimi", "codex"]), candidates_)
    assert out.executor == "opencode/kimi"


def test_post_fallback_chain_exhausted_keeps_top1_with_warning():
    from agent_dispatch.models import RouteDecision, RouterKind

    d = RouteDecision(
        router=RouterKind.jev, executor="claude", confidence=0.5, scores={"claude": 0.9}
    )
    out, events = post_guards(d, chain_settings(["opencode/kimi", "codex"]), ["claude"])
    assert out.executor == "claude"
    assert out.meta["warning"] == "fallback executor unavailable: opencode/kimi, codex"
    assert events[-1].reason == GuardReason.unavailable


def perimeter_settings(chain):
    return Settings.model_validate(
        {
            "routing": {"fallback_executor": chain},
            "executors": {
                "internal": {"adapter": "opencode", "model": "m", "corporate": True},
                "codex": {"adapter": "codex"},
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

    s = perimeter_settings(["codex"])
    d = RouteDecision(
        router=RouterKind.jev,
        executor="internal",
        confidence=0.3,
        scores={"internal": 0.9},
        judgments={"corporate_data": _corporate_judgment()},
    )
    out, events = post_guards(d, s, ["internal", "codex"])
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
    out, _ = post_guards(d, perimeter_settings(["codex"]), ["internal", "codex", "claude"])
    assert out.executor == "internal"
    assert out.meta["perimeter"] == "unknown"
    assert any("fail closed" in note for note in out.meta["selection_notes"])


def test_post_weak_without_corporate_in_chain_stays_inside_perimeter():
    """В цепочке нет исполнителя периметра: выбор остаётся в пуле, с предупреждением."""
    from agent_dispatch.models import RouteDecision, RouterKind

    s = perimeter_settings(["codex"])
    d = RouteDecision(
        router=RouterKind.jev,
        executor="claude",
        confidence=0.3,
        scores={"claude": 0.9, "internal": 0.1},
        judgments={"corporate_data": _corporate_judgment()},
    )
    out, events = post_guards(d, s, ["internal", "claude", "codex"])
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
                "cheap": {"adapter": "codex", "tier": "fast"},
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
                    "adapter": "opencode",
                    "model": "m",
                    "tier": "strong",
                    "corporate": True,
                },
                "external": {"adapter": "codex", "tier": "fast"},
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
                "external": {"adapter": "codex", "tier": "balanced"},
                "internal": {
                    "adapter": "opencode",
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
