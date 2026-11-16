from __future__ import annotations

import ast
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASKS = {
    "fix": "tests/test_calc.py fails; find the bug in smoke_target/calc.py and fix it; run pytest",
    "docstring": (
        "add a docstring to add() in smoke_target/calc.py describing arguments and return value"
    ),
    "rename": (
        "rename function add to add_numbers in smoke_target/calc.py and update "
        "tests/test_calc.py; run pytest"
    ),
    "decisions_fork": (
        "add a function to smoke_target/calc.py that converts a date string in "
        "'YYYY-MM-DD' form into a datetime.date; two equally good names for it are "
        "parse_date and to_date — pick either one; run pytest"
    ),
    "decisions_none": (
        "add a function sub(a, b) to smoke_target/calc.py that returns a - b, and a test "
        "sub(2, 3) == -1 in tests/test_calc.py; run pytest"
    ),
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: list[str]


# Пороги eval'а записей решений (docs/plans/20261115-decision-log.md): N=10 прогонов
# каждой задачи на исполнителя, позитив не ниже 80%, шум не выше 20% серии. Доли,
# а не абсолютные 8 и 2: при --runs 20 позитив 8/20 иначе проходил бы порог.
DECISIONS_RUNS_PER_EXECUTOR = 10
DECISIONS_POSITIVE_SHARE = 0.8
DECISIONS_NOISE_SHARE = 0.2


def _mentions_fork(text: str) -> bool:
    """Запись называет выбор имени API из задачи: parse_date или to_date.

    Слово «fork» или «развилка» само по себе не доказывает, что записан именно
    выбор имени («chose fork over subprocess» прошёл бы порог без нужного решения).
    """
    lowered = text.lower()
    return "parse_date" in lowered or "to_date" in lowered


def decisions_counts(results: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Позитив, шум и число прогонов каждой задачи по готовым результатам.

    Позитив — прогон decisions_fork, где среди result.meta.decisions есть запись
    kind=decision, чей text называет выбранное имя или развилку. Шум — прогон
    decisions_none с непустым meta.decisions. Ровно эти ключи кладёт
    make_row/__main__ при живом прогоне; функция также принимает готовые строки
    отчёта, где decisions лежат плоским ключом.
    """
    counts: dict[str, dict[str, int]] = {}
    for result in results:
        per_executor = counts.setdefault(
            str(result.get("executor", "")),
            {"positive": 0, "noise": 0, "fork_runs": 0, "none_runs": 0, "failed_runs": 0},
        )
        # Упавший прогон остаётся в серии, но всегда в худшую сторону: развилка без
        # позитива, тривиальная задача как шум. Иначе проваленный decisions_none давал
        # бы «шум 0», а один обрыв транспорта не должен обнулять всю серию
        # (eval 2026-11-16: 9/9 успешных прогонов x5 провалили бы строгий порог).
        failed = not result.get("ok", True)
        per_executor["failed_runs"] += failed
        meta = result.get("meta") or {}
        decisions = meta.get("decisions") or result.get("decisions") or []
        task = result.get("task")
        if task == "decisions_fork":
            per_executor["fork_runs"] += 1
            if not failed and any(
                decision.get("kind") == "decision" and _mentions_fork(str(decision.get("text", "")))
                for decision in decisions
            ):
                per_executor["positive"] += 1
        elif task == "decisions_none":
            per_executor["none_runs"] += 1
            if failed or decisions:
                per_executor["noise"] += 1
    return counts


def decisions_threshold_ok(
    counts: Mapping[str, Mapping[str, int]],
    *,
    runs: int = DECISIONS_RUNS_PER_EXECUTOR,
    positive_min: int | None = None,
    noise_max: int | None = None,
) -> bool:
    """Порог: у каждого исполнителя серия из N попыток каждой задачи, позитив не ниже
    positive_min, шум не выше noise_max (по умолчанию 80% и 20% от N). Упавшие прогоны
    уже посчитаны в худшую сторону; неполная серия — fail, иначе 2 из 2 выглядели бы
    как 10 из 10."""
    if positive_min is None:
        positive_min = math.ceil(DECISIONS_POSITIVE_SHARE * runs)
    if noise_max is None:
        noise_max = math.floor(DECISIONS_NOISE_SHARE * runs)
    return bool(counts) and all(
        per_executor["fork_runs"] == runs
        and per_executor["none_runs"] == runs
        and per_executor["positive"] >= positive_min
        and per_executor["noise"] <= noise_max
        for per_executor in counts.values()
    )


def make_row(task: str, executor: str, verdict: Verdict, view: Any) -> dict[str, Any]:
    """Строка отчёта: meta.decisions из result исполнителя рядом с вердиктом.

    Без meta.decisions строку не посчитать: у TaskView.result решения лежат
    в result.meta.decisions (models.py), плоского поля decisions нет.
    """
    result = getattr(view, "result", None)
    meta = getattr(result, "meta", None) or {}
    decisions = meta.get("decisions")
    if decisions is None and isinstance(result, Mapping):
        decisions = result.get("decisions")
    row: dict[str, Any] = {
        "executor": executor,
        "task": task,
        "ok": verdict.ok,
        "reasons": verdict.reasons,
    }
    if decisions is not None:
        # Готово к json.dumps: записи {kind, text, why} — только JSON-типы.
        row["meta"] = {"decisions": json.loads(json.dumps(decisions))}
    return row


BUGGY_CALC = "def add(a, b):\n    return a - b\n"


def prepare_repo(template: Path, destination: Path, *, inject_bug: bool = False) -> Path:
    """Копия шаблона с git-коммитом.

    Шаблон корректен; баг в add() (вычитание вместо сложения) вносится только
    для задачи fix, иначе docstring/rename проваливались бы по pytest по построению.
    """
    shutil.copytree(template, destination)
    if inject_bug:
        (destination / "smoke_target/calc.py").write_text(BUGGY_CALC)
    subprocess.run(["git", "init"], cwd=destination, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=destination, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=eval@example.com",
            "-c",
            "user.name=eval",
            "commit",
            "-m",
            "baseline",
        ],
        cwd=destination,
        check=True,
        capture_output=True,
    )
    return destination


def verify(repo: Path, view: Any, task: str) -> Verdict:
    reasons: list[str] = []
    if getattr(view, "status", None) != "completed":
        reasons.append(f"status={getattr(view, 'status', None)}")
    if not getattr(view, "result", None) or not getattr(view.result, "changed_files", []):
        reasons.append("no changed files")
    with tempfile.TemporaryDirectory(prefix="agent-dispatch-pycache-") as cache:
        env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONPYCACHEPREFIX": cache}
        pytest_run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
        )
        if pytest_run.returncode != 0:
            reasons.append("pytest failed")
    calc_path = repo / "smoke_target/calc.py"
    try:
        calc_tree = ast.parse(calc_path.read_text(), filename=str(calc_path))
    except (OSError, SyntaxError) as exc:
        # Исполнитель мог удалить или сломать файл: это провал задачи, не харнесса.
        reasons.append(f"calc.py unreadable: {type(exc).__name__}: {exc}")
        return Verdict(ok=False, reasons=reasons)
    functions = {
        node.name: node for node in ast.walk(calc_tree) if isinstance(node, ast.FunctionDef)
    }

    if task == "fix":
        _check_calculation(repo, "add", reasons)
        with tempfile.TemporaryDirectory(prefix="agent-dispatch-reference-") as raw:
            reference = Path(raw) / "test_calc.py"
            shutil.copy2(Path(__file__).parent / "repo_template/tests/test_calc.py", reference)
            with tempfile.TemporaryDirectory(prefix="agent-dispatch-pycache-") as cache:
                reference_run = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", str(reference)],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    env={**env, "PYTHONPYCACHEPREFIX": cache},
                )
            if reference_run.returncode != 0:
                reasons.append("reference pytest failed")
    elif task == "docstring":
        # Задача просит докстринг именно у add(); переименованная функция не считается.
        function = functions.get("add")
        if function is None or not ast.get_docstring(function):
            reasons.append("missing docstring on add()")
    elif task == "rename":
        try:
            tests_tree = ast.parse((repo / "tests/test_calc.py").read_text())
        except (OSError, SyntaxError) as exc:
            reasons.append(f"test_calc.py unreadable: {type(exc).__name__}: {exc}")
            return Verdict(ok=False, reasons=reasons)
        if "add_numbers" not in functions:
            reasons.append("add_numbers not defined")
        else:
            _check_calculation(repo, "add_numbers", reasons)
        if "add" in functions:
            reasons.append("old add definition remains")
        if not _tests_import_add_numbers(tests_tree):
            reasons.append("tests do not import add_numbers")
        if any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "add"
            for node in ast.walk(tests_tree)
        ):
            reasons.append("tests still call add")
    elif task == "decisions_fork":
        # Имя выбирает исполнитель, поведение функции — нет: `return s` без
        # проверки даты не считается. Сравнение по ISO-строке: JSON-совместимо.
        chosen = next((name for name in ("parse_date", "to_date") if name in functions), None)
        if chosen is None:
            reasons.append("no parse_date/to_date function defined")
        else:
            _check_calculation(
                repo,
                chosen,
                reasons,
                calls=(("2026-11-16",),),
                expected=("2026-11-16",),
                returns_date=True,
            )
    elif task == "decisions_none":
        if "sub" not in functions:
            reasons.append("sub not defined")
        else:
            _check_calculation(repo, "sub", reasons, calls=((2, 3), (5, 1)), expected=(-1, 4))
    return Verdict(not reasons, reasons)


def _check_calculation(
    repo: Path,
    function_name: str,
    reasons: list[str],
    calls: tuple[tuple[object, ...], ...] = ((2, 3), (-1, 1)),
    expected: tuple[object, ...] = (5, 0),
    returns_date: bool = False,
) -> None:
    script = """
import importlib.util
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("smoke_target.calc_eval", path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
source = spec.loader.get_source(spec.name)
assert source is not None
exec(compile(source, str(path), "exec"), module.__dict__)
function = getattr(module, sys.argv[2])
import datetime
args = json.loads(sys.argv[3])
expected = json.loads(sys.argv[4])
returns_date = sys.argv[5] == "1"
for call, want in zip(args, expected):
    got = function(*call)
    if returns_date:
        # `return s` без разбора даты не проходит: нужен datetime.date.
        if not isinstance(got, datetime.date):
            raise AssertionError(f"{sys.argv[2]} returned {type(got).__name__}, not a date")
        got = got.isoformat()
    if got != want:
        # Прежний формат причины («add(2, 3) != 5»): его читают отчёты и тесты.
        shown = ", ".join(str(value) for value in call)
        raise AssertionError(f"{sys.argv[2]}({shown}) != {want}")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(repo / "smoke_target/calc.py"),
            function_name,
            json.dumps([list(call) for call in calls]),
            json.dumps(list(expected)),
            "1" if returns_date else "0",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
        reasons.append(detail.removeprefix("AssertionError: ") or f"{function_name} check failed")


def _tests_import_add_numbers(tree: ast.AST) -> bool:
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "smoke_target.calc":
            if any(alias.name == "add_numbers" for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom) and node.module == "smoke_target":
            module_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name == "calc"
            )
        if isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or "smoke_target"
                for alias in node.names
                if alias.name == "smoke_target.calc"
            )
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "add_numbers"
        and (
            isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
            or isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "smoke_target"
            and node.value.attr == "calc"
            and "smoke_target" in module_aliases
        )
        for node in ast.walk(tree)
    )
