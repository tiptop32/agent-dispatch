import sys
import tempfile
from pathlib import Path

import pytest

from evals.executors.__main__ import main
from evals.executors.harness import load_cases, replay, run_case

CORPUS = Path(__file__).parents[1] / "evals/executors/corpus.jsonl"


def test_evaluator_catches_deliberately_wrong_expectation():
    """Негативный контроль: заведомо неверное ожидание обязано провалиться,
    причём по нужной причине — иначе реплей не ловит расхождения с контрактом."""
    cases = load_cases(CORPUS)
    victim = next(case for case in cases if case["kind"] == "argv")
    broken = {
        **victim,
        "id": f"{victim['id']}_broken",
        "expected": {**victim["expected"], "tail": ["--surely-not-a-real-flag", "task"]},
    }

    report = replay([*cases, broken])

    assert not report["pass"]
    failures = {row["id"]: row for row in report["failures"]}
    assert list(failures) == [broken["id"]]
    reason = failures[broken["id"]]["reason"]
    assert reason.startswith("argv tail ") and "'task'" in reason


def test_forbidden_flag_negative_control():
    """Негативный контроль forbidden: флаг, который адаптер всегда ставит,
    обязан провалить кейс с причиной 'argv contains forbidden' — иначе
    forbidden-проверка в реплее не ловит ничего."""
    cases = load_cases(CORPUS)
    victim = next(case for case in cases if case["kind"] == "argv")
    probe = {
        **victim,
        "id": f"{victim['id']}_forbidden_probe",
        "expected": {**victim["expected"], "forbidden": ["--model"]},
    }

    with tempfile.TemporaryDirectory() as tmp:
        row = run_case(probe, Path(tmp))

    assert not row.ok
    assert "argv contains forbidden" in row.reason


def test_corpus_replay_fully_passes():
    report = replay(load_cases(CORPUS))
    assert report["failures"] == [] and report["pass"], report["failures"]
    assert report["cases"] == report["passed"]


def test_eval_cli_uses_module_relative_corpus_outside_repo(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["python -m evals.executors"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 0
    expected = len(load_cases(CORPUS))
    assert f"{expected}/{expected} passed" in capsys.readouterr().out
