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
    req = DispatchRequest(task="x", cwd=str(git_repo), executor="codex")
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
