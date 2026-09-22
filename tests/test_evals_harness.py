from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.routing.__main__ import select_routers
from evals.routing.harness import build_request, load_cases, render, score
from evals.smoke.harness import TASKS, prepare_repo, verify

CASES = "evals/routing/cases.jsonl"


def test_load_cases_has_twenty_rows_and_required_fields():
    cases = load_cases(Path(CASES))
    assert len(cases) == 20
    assert {case["expected_executor"] for case in cases} == {
        "claude",
        "codex",
        "opencode/kimi",
        "opencode/x5-code",
    }
    assert all(case["task"] and case["rationale"] for case in cases)


def test_load_cases_rejects_missing_field(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"id": "x"}) + "\n")
    with pytest.raises(ValueError, match="missing fields"):
        load_cases(path)


def test_build_request_maps_case_fields(tmp_path):
    request = build_request(
        {"task": "fix it", "context": "ctx", "files": ["a.py"], "constraints": ["tests"]},
        "cli",
    )
    assert request.task == "fix it"
    assert request.context == "ctx"
    assert request.files == ["a.py"]
    assert request.constraints == ["tests"]
    assert request.source_agent == "cli"


def test_build_request_uses_explicit_cwd(tmp_path):
    request = build_request({"task": "x", "cwd": str(tmp_path)}, "cli")
    assert request.cwd == str(tmp_path)


def test_score_accuracy_confusion_cost_and_p50():
    summary = score(
        [
            {"expected": "a", "got": "a", "cost_usd": 1, "latency_ms": 10},
            {"expected": "a", "got": "b", "cost_usd": 2, "latency_ms": 20},
            {"expected": "b", "got": "b", "cost_usd": 0, "latency_ms": 30},
            {"expected": "b", "got": "a", "cost_usd": 1, "latency_ms": 40},
        ]
    )
    assert summary.accuracy == 0.5
    assert summary.confusion == {"a": {"a": 1, "b": 1}, "b": {"a": 1, "b": 1}}
    assert summary.cost == 4
    assert summary.latency_p50 == 25


def test_score_empty_is_zero():
    summary = score([])
    assert summary.accuracy == summary.cost == summary.latency_p50 == 0
    assert summary.confusion == {}


def test_render_contains_accuracy_and_matrix():
    output = render(score([{"expected": "claude", "got": "codex", "latency_ms": 3}]))
    assert "accuracy: 0.0%" in output
    assert "claude" in output and "codex=1" in output


def test_threshold_can_fail():
    assert score([{"expected": "a", "got": "b"}]).accuracy < 0.8


def test_prepare_repo_creates_commit_and_files(tmp_path):
    destination = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    assert (destination / ".git").is_dir()
    assert (destination / "smoke_target/calc.py").is_file()
    result = subprocess.run(
        ["git", "log", "-1", "--oneline"], cwd=destination, capture_output=True, text=True
    )
    assert result.returncode == 0 and "baseline" in result.stdout
    assert (destination / ".gitignore").read_text().splitlines() == [
        "__pycache__/",
        ".pytest_cache/",
    ]
    pytest_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=destination,
        capture_output=True,
        text=True,
    )
    assert pytest_run.returncode != 0  # The template deliberately contains the bug.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=destination, capture_output=True, text=True
    )
    assert status.returncode == 0 and status.stdout == ""


def _view(status="completed", changed_files=None):
    return SimpleNamespace(
        status=status,
        result=SimpleNamespace(changed_files=changed_files or ["smoke_target/calc.py"]),
    )


def test_verify_fix_passes_after_fix(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text("def add(a, b):\n    return a + b\n")
    verdict = verify(repo, _view(), "fix")
    assert verdict.ok, verdict.reasons


def test_verify_fix_with_empty_path(tmp_path, monkeypatch):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text("def add(a, b):\n    return a + b\n")
    monkeypatch.setenv("PATH", "")
    verdict = verify(repo, _view(), "fix")
    assert verdict.ok, verdict.reasons


def test_verify_fix_ignores_tampered_agent_test_and_checks_calc(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests/test_calc.py").write_text(
        "from smoke_target.calc import add\n\ndef test_add():\n    assert add(2, 3) == -1\n"
    )
    verdict = verify(repo, _view(), "fix")
    assert not verdict.ok
    assert "add(2, 3) != 5" in verdict.reasons


def test_verify_failed_view_is_not_ok(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    verdict = verify(repo, _view(status="failed"), "fix")
    assert not verdict.ok
    assert "status=failed" in verdict.reasons


def test_verify_docstring_requires_triple_quotes(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text("def add(a, b):\n    return a + b\n")
    verdict = verify(repo, _view(), "docstring")
    assert not verdict.ok
    assert "missing docstring on add()" in verdict.reasons


def test_verify_docstring_accepts_single_triple_quotes_and_checks_add(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text(
        "def add(a, b):\n    '''sum values'''\n    return a + b\n"
        '\ndef other():\n    """wrong function"""\n'
    )
    verdict = verify(repo, _view(), "docstring")
    assert verdict.ok, verdict.reasons


def test_verify_docstring_on_other_function_is_rejected(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text(
        'def add(a, b):\n    return a + b\n\ndef other():\n    """unrelated"""\n'
    )
    verdict = verify(repo, _view(), "docstring")
    assert "missing docstring on add()" in verdict.reasons


def test_verify_rename_requires_new_definition(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text("def add(a, b):\n    return a + b\n")
    verdict = verify(repo, _view(), "rename")
    assert not verdict.ok
    assert "add_numbers not defined" in verdict.reasons


def test_verify_rename_rejects_old_name_and_unupdated_tests(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef add_numbers(a, b):\n    return a + b\n"
    )
    verdict = verify(repo, _view(), "rename")
    assert not verdict.ok
    assert "old add definition remains" in verdict.reasons
    assert "tests do not import add_numbers" in verdict.reasons


def test_verify_rename_requires_updated_tests_and_correct_result(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    calc = repo / "smoke_target/calc.py"
    calc.write_text("def add_numbers(a, b):\n    return a - b\n")
    verdict = verify(repo, _view(), "rename")
    assert "tests do not import add_numbers" in verdict.reasons
    assert "tests still call add" in verdict.reasons
    assert "add_numbers(2, 3) != 5" in verdict.reasons

    calc.write_text("def add_numbers(a, b):\n    return a + b\n")
    (repo / "tests/test_calc.py").write_text(
        "from smoke_target.calc import add_numbers\n\ndef test_add():\n"
        "    assert add_numbers(2, 3) == 5\n"
    )
    verdict = verify(repo, _view(), "rename")
    assert verdict.ok, verdict.reasons


def test_select_routers_returns_requested_in_process_router(monkeypatch):
    from agent_dispatch.config import load_settings

    selected = select_routers(load_settings(), "claude_local")
    assert [router.name for router in selected] == ["claude_local"]


def test_routing_daemon_uses_client(tmp_path, monkeypatch):
    import asyncio

    import evals.routing.__main__ as routing_main
    from agent_dispatch.models import RouteDecision, RouterKind

    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "id": "one",
                "task": "route",
                "context": "",
                "files": [],
                "constraints": [],
                "expected_executor": "codex",
                "rationale": "test",
            }
        )
        + "\n"
    )
    calls = []

    class FakeClient:
        def __init__(self, state):
            calls.append(("client", state))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def route(self, request):
            calls.append(("route", request.task))
            return RouteDecision(executor="codex", confidence=1, scores={}, router=RouterKind.jev)

    async def daemon(settings):
        calls.append(("daemon", None))
        return "state"

    monkeypatch.setattr(routing_main, "ensure_daemon", daemon)
    monkeypatch.setattr(routing_main, "DispatchClient", FakeClient)
    monkeypatch.setattr(routing_main, "select_routers", lambda *args: pytest.fail("in-process"))
    args = SimpleNamespace(
        cases=str(cases),
        limit=None,
        source_agent="cli",
        router="daemon",
        threshold=0.8,
        report_dir=str(tmp_path / "reports"),
    )
    assert asyncio.run(routing_main.run(args)) == 0
    assert calls == [("daemon", None), ("client", "state"), ("route", "route")]
    report = next((tmp_path / "reports").glob("*.json"))
    assert json.loads(report.read_text())["router_mode"] == "daemon"


def test_routing_help_exposes_router_option():
    result = subprocess.run(
        [sys.executable, "-m", "evals.routing", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--router" in result.stdout


def test_smoke_tasks_are_complete():
    assert set(TASKS) == {"fix", "docstring", "rename"}


def test_verify_broken_calc_is_failed_verdict_not_exception(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text("def add(a, b:\n    return a + b\n")
    verdict = verify(repo, _view(), "fix")
    assert not verdict.ok
    assert any("calc.py unreadable" in reason for reason in verdict.reasons)


def test_verify_missing_calc_is_failed_verdict(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").unlink()
    verdict = verify(repo, _view(), "docstring")
    assert not verdict.ok


def test_verify_docstring_rejects_renamed_function(tmp_path):
    repo = prepare_repo(Path("evals/smoke/repo_template"), tmp_path / "repo")
    (repo / "smoke_target/calc.py").write_text(
        'def add_numbers(a, b):\n    """Sum a and b."""\n    return a + b\n'
    )
    (repo / "tests/test_calc.py").write_text(
        "from smoke_target.calc import add_numbers\n\n\ndef test_add():\n"
        "    assert add_numbers(2, 3) == 5\n"
    )
    verdict = verify(repo, _view(), "docstring")
    assert not verdict.ok
    assert "missing docstring on add()" in verdict.reasons


def test_smoke_main_rejects_unknown_task_and_accepts_known(monkeypatch, capsys):
    import evals.smoke.__main__ as smoke_main

    monkeypatch.setattr("sys.argv", ["smoke", "--tasks", "fix,bogus"])
    with pytest.raises(SystemExit) as exc:
        smoke_main.main()
    assert exc.value.code == 2
    assert "unknown tasks" in capsys.readouterr().err

    async def fake_run(args):
        assert args.tasks == "fix,docstring"
        return 0

    monkeypatch.setattr(smoke_main, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["smoke", "--tasks", "fix,docstring"])
    with pytest.raises(SystemExit) as exc:
        smoke_main.main()
    assert exc.value.code == 0
