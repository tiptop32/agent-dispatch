"""scripts/ad-external-review: контракт external_review_cmd плагина planning."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ad-external-review"


def _fake_cli(tmp_path: Path, views: list[dict]) -> Path:
    """Поддельный agent-dispatch: по очереди отдаёт views и пишет argv в calls.jsonl."""
    (tmp_path / "views.json").write_text(json.dumps(views))
    fake = tmp_path / "fake-ad"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, pathlib\n"
        f"d = pathlib.Path({str(tmp_path)!r})\n"
        "calls = d / 'calls.jsonl'\n"
        "n = len(calls.read_text().splitlines()) if calls.exists() else 0\n"
        "with calls.open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "views = json.loads((d / 'views.json').read_text())\n"
        "print(json.dumps(views[min(n, len(views) - 1)]))\n"
    )
    fake.chmod(0o755)
    return fake


def _run(tmp_path: Path, views: list[dict], prompt: str = "review the plan"):
    fake = _fake_cli(tmp_path, views)
    env = {**os.environ, "AD_DISPATCH": str(fake)}
    out = subprocess.run(
        [str(SCRIPT), prompt],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    calls = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    return out, calls


def _view(status: str, summary: str | None = None, error: str | None = None) -> dict:
    return {"status": status, "result": {"summary": summary, "error": error}}


def test_tagged_findings_go_to_stdout_and_exit_zero(tmp_path: Path) -> None:
    summary = "- CRITICAL a.py:1 broken\nnoise line\nMAJOR b.py:2 fix\n* MINOR c.py:3 nit"
    out, calls = _run(tmp_path, [_view("completed", summary)])

    assert out.returncode == 0
    assert out.stdout.splitlines() == [
        "CRITICAL a.py:1 broken",
        "MAJOR b.py:2 fix",
        "MINOR c.py:3 nit",
    ]
    argv = calls[0]
    assert argv[:2] == ["dispatch", "--executor"] and argv[2] == "omp/sol"
    assert {"--kind", "review", "--no-escalation", "--json"} <= set(argv)
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[-1].startswith("review the plan") and "NO ISSUES FOUND" in argv[-1]


def test_no_issues(tmp_path: Path) -> None:
    out, _ = _run(tmp_path, [_view("completed", "NO ISSUES FOUND")])
    assert (out.returncode, out.stdout.strip()) == (0, "NO ISSUES FOUND")


def test_untagged_review_is_not_lost(tmp_path: Path) -> None:
    out, _ = _run(tmp_path, [_view("completed", "the parser drops quotes")])
    assert out.returncode == 0
    assert out.stdout.startswith("MAJOR reviewer output without severity tags: the parser")


def test_transport_failure_retried_once(tmp_path: Path) -> None:
    views = [
        _view("failed", error="Upstream websocket closed before response.completed"),
        _view("completed", "MINOR x.py:1 ok"),
    ]
    out, calls = _run(tmp_path, views)
    assert out.returncode == 0 and out.stdout.strip() == "MINOR x.py:1 ok"
    assert len(calls) == 2


def test_other_failure_not_retried_and_exit_nonzero(tmp_path: Path) -> None:
    out, calls = _run(tmp_path, [_view("failed", error="guard: unavailable")])
    assert out.returncode == 1
    assert "guard: unavailable" in out.stderr
    assert out.stdout == "" and len(calls) == 1


def test_executor_override(tmp_path: Path) -> None:
    fake = _fake_cli(tmp_path, [_view("completed", "NO ISSUES FOUND")])
    env = {**os.environ, "AD_DISPATCH": str(fake), "AD_REVIEW_EXECUTOR": "omp/terra"}
    subprocess.run([str(SCRIPT), "p"], env=env, cwd=tmp_path, capture_output=True, timeout=30)
    argv = json.loads((tmp_path / "calls.jsonl").read_text().splitlines()[0])
    assert argv[argv.index("--executor") + 1] == "omp/terra"
