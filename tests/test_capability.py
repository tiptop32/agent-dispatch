import pytest

from agent_dispatch.config import ExecutorSettings, Settings
from agent_dispatch.routing.capability import has_corporate, select, tiers_present
from agent_dispatch.routing.guards import classify_confidence
from agent_dispatch.routing.questions import noul_certainty, noul_yes


def ex(adapter="codex", tier="balanced", corporate=False):
    return ExecutorSettings(
        adapter=adapter,
        model="m" if adapter == "opencode" else None,
        tier=tier,
        corporate=corporate,
    )


FULL = {
    "cheap": ex("opencode", "fast"),
    "internal": ex("opencode", "fast", corporate=True),
    "codex": ex("codex", "balanced"),
    "opus": ex("claude", "strong"),
}


def test_tiers_present_lists_only_tiers_with_candidates_in_order():
    assert tiers_present({"a": ex(tier="strong"), "b": ex(tier="fast")}) == ["fast", "strong"]
    assert tiers_present({"a": ex(tier="balanced")}) == ["balanced"]


def test_has_corporate():
    assert has_corporate(FULL) and not has_corporate({"codex": ex()})


@pytest.mark.parametrize(
    ("capability", "expected"),
    [("fast", "cheap"), ("balanced", "codex"), ("strong", "opus")],
)
def test_each_capability_picks_its_tier(capability, expected):
    assert select(capability, FULL).executor == expected


def test_missing_tier_falls_upward_then_downward():
    only_strong_and_fast = {"cheap": ex("opencode", "fast"), "opus": ex("claude", "strong")}
    # balanced нет: берём следующий вверх, а не вниз, чтобы задача не провалилась.
    assert select("balanced", only_strong_and_fast).executor == "opus"
    only_fast = {"cheap": ex("opencode", "fast")}
    assert select("strong", only_fast).executor == "cheap"


def test_corporate_note_carries_the_confidence_so_a_coin_flip_is_visible():
    selection = select("fast", FULL, corporate=True, corporate_confidence=0.06)

    assert any("confidence 0.06" in note for note in selection.notes)


def test_perimeter_without_the_requested_level_says_so_instead_of_silently_downgrading():
    # Внутри периметра только fast, а задача просит strong: раньше она молча
    # уезжала на соседний тир, и понять это по решению было нельзя.
    pool = {"internal": ex("opencode", "fast", corporate=True), "opus": ex("claude", "strong")}

    selection = select("strong", pool, corporate=True)

    assert selection.executor == "internal"
    assert any("requested level 'strong'" in note for note in selection.notes)


def test_corporate_data_restricts_the_pool():
    selection = select("fast", FULL, corporate=True)
    assert selection.executor == "internal"
    assert any("corporate" in note for note in selection.notes)


def test_corporate_data_without_an_internal_executor_is_reported_not_silently_ignored():
    external_only = {"codex": ex(), "opus": ex("claude", "strong")}
    selection = select("balanced", external_only, corporate=True)
    assert selection.executor == "codex"
    assert any("no in-perimeter executor" in note for note in selection.notes)


def test_judgment_prefers_a_reasoning_agent_within_the_pool():
    selection = select("balanced", FULL, judgment=True)
    assert selection.executor == "opus"
    assert any("judgment" in note for note in selection.notes)


def test_corporate_wins_over_judgment_because_the_perimeter_is_not_negotiable():
    selection = select("strong", FULL, judgment=True, corporate=True)
    assert selection.executor == "internal"


def test_ties_are_broken_by_config_order():
    ordered = {"first": ex("codex", "fast"), "second": ex("codex", "fast")}
    assert select("fast", ordered).executor == "first"


def test_scores_map_tier_probabilities_onto_representatives():
    scores = select(
        "fast", FULL, probabilities={"fast": 0.7, "balanced": 0.2, "strong": 0.1}
    ).scores
    assert scores["cheap"] == pytest.approx(0.7)
    assert scores["codex"] == pytest.approx(0.2)
    assert scores["opus"] == pytest.approx(0.1)


def test_select_without_candidates_is_an_error_not_a_silent_default():
    with pytest.raises(ValueError, match="no candidates"):
        select("fast", {})


def settings(**routing):
    return Settings.model_validate(
        {"executors": {"codex": {"adapter": "codex"}}, "routing": routing}
    )


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (0.95, "autonomous"),
        (0.85, "autonomous"),
        (0.7, "advisory"),
        (0.6, "advisory"),
        (0.4, "fallback"),
    ],
)
def test_confidence_tiers(confidence, expected):
    assert classify_confidence(confidence, settings()) == expected


def test_thresholds_are_configurable_and_must_be_ordered():
    assert classify_confidence(0.8, settings(autonomous_confidence=0.75)) == "autonomous"
    with pytest.raises(Exception, match="autonomous_confidence"):
        settings(min_confidence=0.9, autonomous_confidence=0.5)


@pytest.mark.parametrize(
    ("probability", "certainty"), [(0.5, 0.0), (1.0, 1.0), (0.0, 1.0), (0.83, 0.66), (0.28, 0.44)]
)
def test_noul_certainty_treats_half_as_unknown(probability, certainty):
    assert noul_certainty(probability) == pytest.approx(certainty)


def test_noul_yes():
    assert noul_yes(0.5) and noul_yes(0.9) and not noul_yes(0.49)
