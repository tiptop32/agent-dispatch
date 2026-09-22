from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
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
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: list[str]


def prepare_repo(template: Path, destination: Path) -> Path:
    shutil.copytree(template, destination)
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
        if "add" in functions:
            reasons.append("old add definition remains")
        if not _tests_import_add_numbers(tests_tree):
            reasons.append("tests do not import add_numbers")
        if any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "add"
            for node in ast.walk(tests_tree)
        ):
            reasons.append("tests still call add")
        _check_calculation(repo, "add_numbers", reasons)
    return Verdict(not reasons, reasons)


def _check_calculation(repo: Path, function_name: str, reasons: list[str]) -> None:
    script = """
import importlib.util
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
if function(2, 3) != 5:
    raise AssertionError(f"{sys.argv[2]}(2, 3) != 5")
if function(-1, 1) != 0:
    raise AssertionError(f"{sys.argv[2]}(-1, 1) != 0")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo / "smoke_target/calc.py"), function_name],
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
