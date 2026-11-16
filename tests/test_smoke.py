import json
import re
from types import SimpleNamespace

import agent_dispatch
from evals.smoke.harness import (
    TASKS,
    Verdict,
    decisions_counts,
    decisions_threshold_ok,
    make_row,
    verify,
)


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", agent_dispatch.__version__)


def test_git_repo_fixture_has_commit(git_repo) -> None:
    assert (git_repo / ".git").is_dir()
    assert (git_repo / "a.py").read_text() == "x = 1\n"


def _row(
    executor: str = "omp/sol",
    task: str = "decisions_fork",
    decisions: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Готовая строка отчёта: как её пишет make_row, meta.decisions внутри."""
    row: dict[str, object] = {"executor": executor, "task": task, "ok": True, "reasons": []}
    if decisions is not None:
        row["meta"] = {"decisions": decisions}
    return row


def _fork_row(executor: str = "omp/sol") -> dict[str, object]:
    return _row(
        executor,
        decisions=[{"kind": "decision", "text": "chose parse_date over to_date", "why": ""}],
    )


def test_decisions_counts_empty_results() -> None:
    assert decisions_counts([]) == {}


def test_decisions_counts_positive_on_fork_mention() -> None:
    counts = decisions_counts([_fork_row("omp/sol")])
    assert counts == {"omp/sol": {"positive": 1, "noise": 0, "fork_runs": 1, "none_runs": 0}}


def test_decisions_counts_positive_accepts_russian_and_flat_decisions() -> None:
    row = _row("omp/x5-code-large", decisions=[])  # meta без decisions
    row["decisions"] = [{"kind": "decision", "text": "выбрал развилку имён: to_date"}]
    counts = decisions_counts([row])
    assert counts["omp/x5-code-large"]["positive"] == 1


def test_decisions_counts_ignores_other_kinds_and_unrelated_text() -> None:
    row = _row(
        decisions=[
            {"kind": "question", "text": "parse_date or to_date?"},
            {"kind": "decision", "text": "added the function"},
        ]
    )
    assert decisions_counts([row])["omp/sol"]["positive"] == 0


def test_mentions_fork_rejects_unrelated_fork_words() -> None:
    # 'fork' как подстрока ловит 'forked repo' и 'forkserver': нужен целый словоформ.
    from evals.smoke.harness import _mentions_fork

    assert not _mentions_fork("used forkserver internally")
    assert not _mentions_fork("forked the repo first")
    assert _mentions_fork("chose fork over subprocess")
    assert not _mentions_fork("no naming choice here")


def test_decisions_counts_noise_on_trivial_task_with_decisions() -> None:
    counts = decisions_counts(
        [
            _row("omp/sol", task="decisions_none", decisions=[]),
            {"executor": "omp/sol", "task": "decisions_none", "meta": {"decisions": []}},
            _fork_row(),
        ]
    )
    # Пустой список decisions шумом не считается.
    assert counts["omp/sol"]["noise"] == 0
    assert counts["omp/sol"]["none_runs"] == 2


def test_decisions_threshold_passes_at_boundary() -> None:
    counts = {"omp/sol": {"positive": 8, "noise": 2, "fork_runs": 10, "none_runs": 10}}
    assert decisions_threshold_ok(counts)


def test_decisions_threshold_fails_below_positive() -> None:
    counts = {"omp/sol": {"positive": 7, "noise": 0, "fork_runs": 10, "none_runs": 10}}
    assert not decisions_threshold_ok(counts)


def test_decisions_threshold_fails_above_noise() -> None:
    counts = {"omp/sol": {"positive": 10, "noise": 3, "fork_runs": 10, "none_runs": 10}}
    assert not decisions_threshold_ok(counts)


def test_decisions_threshold_requires_full_series() -> None:
    # 10 из 10 прогонов при неполной серии — это не порог.
    counts = {"omp/sol": {"positive": 10, "noise": 0, "fork_runs": 10, "none_runs": 2}}
    assert not decisions_threshold_ok(counts)
    assert not decisions_threshold_ok({})


def test_decisions_threshold_requires_every_executor() -> None:
    counts = {
        "omp/sol": {"positive": 10, "noise": 0, "fork_runs": 10, "none_runs": 10},
        "omp/x5-code-large": {"positive": 5, "noise": 1, "fork_runs": 10, "none_runs": 10},
    }
    assert not decisions_threshold_ok(counts)


def test_decisions_counts_full_run_n10_meets_threshold() -> None:
    # N=10 прогонов на каждого из двух исполнителей по умолчанию.
    results = []
    for executor in ("omp/sol", "omp/x5-code-large"):
        results += [_fork_row(executor) for _ in range(9)]
        results.append(
            _row(
                executor,
                task="decisions_fork",
                decisions=[{"kind": "decision", "text": "added the function"}],
            )
        )
        noise = [{"kind": "decision", "text": "noted the edit"}]
        results += [_row(executor, task="decisions_none", decisions=noise) for _ in range(2)]
        results += [_row(executor, task="decisions_none") for _ in range(8)]
    counts = decisions_counts(results)
    assert counts == {
        "omp/sol": {"positive": 9, "noise": 2, "fork_runs": 10, "none_runs": 10},
        "omp/x5-code-large": {"positive": 9, "noise": 2, "fork_runs": 10, "none_runs": 10},
    }
    assert decisions_threshold_ok(counts)


def test_make_row_lifts_meta_decisions_from_task_view() -> None:
    # Ровно та сборка, которую делает __main__: решения лежат в result.meta.decisions.
    view = SimpleNamespace(
        status="completed",
        result=SimpleNamespace(
            changed_files=["smoke_target/calc.py"],
            meta={"decisions": [{"kind": "decision", "text": "chose to_date"}]},
        ),
    )
    row = make_row("decisions_fork", "omp/sol", Verdict(ok=True, reasons=[]), view)
    assert row["meta"] == {"decisions": [{"kind": "decision", "text": "chose to_date"}]}
    assert row["ok"] is True
    # Строка переживает json.dumps, как при записи отчёта.
    assert json.loads(json.dumps(row)) == row


def test_make_row_without_decisions_omits_meta() -> None:
    view = SimpleNamespace(status="completed", result=SimpleNamespace(changed_files=[], meta={}))
    row = make_row("decisions_none", "omp/sol", Verdict(ok=False, reasons=["x"]), view)
    assert "meta" not in row
    # И плоский ключ готового отчёта тоже читается.
    row["decisions"] = [{"kind": "decision", "text": "x"}]
    assert decisions_counts([row])["omp/sol"]["noise"] == 1


def test_decisions_tasks_in_tasks_dict() -> None:
    assert {"decisions_fork", "decisions_none"} <= set(TASKS)


def test_verify_decisions_fork_rejects_string_return(tmp_path, monkeypatch) -> None:
    # `return s` без разбора даты — не выполненная задача, ok не должен быть true.
    from evals.smoke import harness

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "smoke_target").mkdir()
    (repo / "smoke_target" / "calc.py").write_text("def parse_date(s):\n    return s\n")
    (repo / "tests").mkdir()
    view = SimpleNamespace(status="completed", result=SimpleNamespace(changed_files=["x"]))
    real_run = harness.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        # Прогон pytest подменяем (репа без зависимостей шаблона не нужна),
        # проверку функции оставляем настоящей: она и есть предмет теста.
        if "-m" in cmd and "pytest" in cmd:
            return SimpleNamespace(returncode=0, stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    verdict = verify(repo, view, "decisions_fork")
    assert not verdict.ok
    assert any("not a date" in reason for reason in verdict.reasons)


def test_verify_decisions_fork_accepts_real_parse(tmp_path, monkeypatch) -> None:
    from evals.smoke import harness

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "smoke_target").mkdir()
    (repo / "smoke_target" / "calc.py").write_text(
        "def parse_date(s):\n"
        "    year, month, day = s.split('-')\n"
        "    return f'{int(year):04d}-{int(month):02d}-{int(day):02d}'\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text("def test_placeholder():\n    assert True\n")
    view = SimpleNamespace(status="completed", result=SimpleNamespace(changed_files=["x"]))
    monkeypatch.setattr(
        harness.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="")
    )
    verdict = verify(repo, view, "decisions_fork")
    assert verdict.ok, verdict.reasons


def test_verify_decisions_none_checks_sub(tmp_path, monkeypatch) -> None:
    from evals.smoke import harness

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "smoke_target").mkdir()
    (repo / "smoke_target" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text("def test_placeholder():\n    assert True\n")
    view = SimpleNamespace(status="completed", result=SimpleNamespace(changed_files=["x"]))
    monkeypatch.setattr(
        harness.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="")
    )
    verdict = verify(repo, view, "decisions_none")
    assert not verdict.ok
    assert "sub not defined" in verdict.reasons


# Живая проверка verify на настоящем шаблоне: офлайн-тесты выше подменяют
# subprocess.run и не заметили, что скрипт сравнивал date со строкой, а sub
# проверялся ожиданиями сложения (2026-11-16).
def _smoke_repo(tmp_path, calc: str):
    from pathlib import Path

    from evals.smoke.harness import prepare_repo

    template = Path(__file__).parent.parent / "evals" / "smoke" / "repo_template"
    repo = prepare_repo(template, tmp_path / "repo")
    (repo / "smoke_target" / "calc.py").write_text(calc)
    view = SimpleNamespace(
        status="completed", result=SimpleNamespace(changed_files=["smoke_target/calc.py"])
    )
    return repo, view


def test_verify_decisions_fork_accepts_real_date_parser(tmp_path) -> None:
    calc = (
        "import datetime\n\n\ndef add(a, b):\n    return a + b\n\n\n"
        "def to_date(s):\n    return datetime.date.fromisoformat(s)\n"
    )
    repo, view = _smoke_repo(tmp_path, calc)
    assert verify(repo, view, "decisions_fork").ok


def test_verify_decisions_fork_rejects_string_passthrough(tmp_path) -> None:
    calc = "def add(a, b):\n    return a + b\n\n\ndef parse_date(s):\n    return s\n"
    repo, view = _smoke_repo(tmp_path, calc)
    verdict = verify(repo, view, "decisions_fork")
    assert not verdict.ok
    assert any("not a date" in reason for reason in verdict.reasons)


def test_verify_decisions_none_checks_subtraction(tmp_path) -> None:
    good = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    repo, view = _smoke_repo(tmp_path, good)
    assert verify(repo, view, "decisions_none").ok
    bad = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a + b\n"
    repo, view = _smoke_repo(tmp_path / "bad", bad)
    assert not verify(repo, view, "decisions_none").ok


def test_main_accepts_executors_argument(monkeypatch) -> None:
    import evals.smoke.__main__ as smoke_main

    seen = {}

    def fake_run(args):
        seen["executors"] = args.executors
        return 0

    monkeypatch.setattr(smoke_main, "run", fake_run)
    monkeypatch.setattr(smoke_main.asyncio, "run", lambda value: value)
    monkeypatch.setattr("sys.argv", ["smoke", "--decisions", "--executors", "omp/sol"])
    try:
        smoke_main.main()
    except SystemExit as exc:
        assert exc.code == 0
    assert seen["executors"] == "omp/sol"
