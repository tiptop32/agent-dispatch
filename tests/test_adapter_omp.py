import json
import os
import stat
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import SecretStr

from agent_dispatch.config import ExecutorSettings, RoutingSettings, Settings
from agent_dispatch.executors.base import RunContext
from agent_dispatch.executors.env import child_env
from agent_dispatch.executors.health import classify_failure
from agent_dispatch.executors.omp import OmpAdapter
from agent_dispatch.executors.process import ProcessOutcome
from agent_dispatch.executors.registry import build_adapters
from agent_dispatch.schemas import load_omp_session_schema

ROOT = Path(__file__).parent
FAKE = ROOT / "fakes" / "omp_ok.sh"
FAKE_ENV = ROOT / "fakes" / "omp_env_capture.sh"
FIXTURES = ROOT / "fixtures" / "omp"

SESSION_ID = "01a19ca4-3448-70f8-a343-fc0df9795e13"


def adapter(**kwargs) -> OmpAdapter:
    settings = ExecutorSettings(
        adapter="omp",
        command=str(kwargs.pop("command", FAKE)),
        model="agent-lb/gpt-5.6-luna",
        **kwargs,
    )
    return OmpAdapter("omp/sol", settings, data_dir="/tmp/agent-dispatch-test")


def ctx(repo, tmp_path, **kwargs) -> RunContext:
    defaults = dict(
        cwd=str(repo),
        timeout_seconds=3,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "run.log",
        task_id="t",
        prompt="do task",
    )
    defaults.update(kwargs)
    return RunContext(**defaults)


def captured_argv(tmp_path) -> list[str]:
    return (tmp_path / "cap.argv").read_text().splitlines()


def captured_env(tmp_path) -> dict[str, str]:
    lines = (tmp_path / "cap.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


@pytest.mark.asyncio
async def test_omp_ok_parses_session_usage_and_cost(git_repo, tmp_path):
    cap = tmp_path / "cap"
    result = await adapter().execute(
        ctx(git_repo, tmp_path, env={"PATH": os.environ["PATH"], "FAKE_CAPTURE": str(cap)})
    )
    assert result.status == "partial"
    assert "PONG" in result.summary
    assert result.meta["parse_error"] == "no result block"
    assert result.usage.input_tokens == 7228
    assert result.usage.output_tokens == 6
    assert result.usage.cost_usd == pytest.approx(0.0014528000000000002)
    assert result.meta["session_id"] == SESSION_ID
    args = captured_argv(tmp_path)
    assert args[:5] == ["-p", "--mode", "json", "--model", "agent-lb/gpt-5.6-luna"]
    assert args[args.index("--thinking") + 1] == "medium"
    assert args[args.index("--max-time") + 1] == "3"
    assert args[args.index("--session-dir") + 1].endswith("omp-sessions")
    assert args[args.index("--cwd") + 1] == str(git_repo)
    assert args[args.index("--config") + 1].endswith("dispatch-overlay.yml")
    assert "--approval-mode" in args and "yolo" in args
    assert args[-1] == "do task"
    # stdin = DEVNULL: omp без TTY ждёт EOF на stdin и висит.
    stdin = tmp_path / "cap.stdin"
    assert stdin.is_file() and stdin.read_text() == ""


@pytest.mark.asyncio
async def test_omp_usage_and_cost_are_summed_over_turns(git_repo, tmp_path):
    # leak_none.ndjson: два ассистентских хода (вызов bash и ответ), у каждого свой usage.
    result = await adapter().execute(
        ctx(git_repo, tmp_path, env={"PATH": os.environ["PATH"], "OMP_FIXTURE": "leak_none.ndjson"})
    )
    assert result.usage.input_tokens == 7416
    assert result.usage.output_tokens == 72
    assert result.usage.cost_usd == pytest.approx(0.00171296)


@pytest.mark.asyncio
async def test_omp_ok_with_report_block_completes(git_repo, tmp_path, monkeypatch):
    from agent_dispatch.executors import omp as module

    #: Последний ассистентский message_end несёт отчётный блок — полный успех.
    report = (
        "```agent-dispatch-result\n"
        '{"status":"completed","summary":"ok","changed_files":["a.py"]}\n'
        "```"
    )
    final = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": report}],
                "usage": {"input": 10, "output": 5, "cost": {"total": 0.01}},
                "stopReason": "stop",
            },
        }
    )
    stdout = (FIXTURES / "ok.ndjson").read_text() + "\n" + final + "\n"

    async def fake_run_cli(*args, **kwargs):
        return ProcessOutcome(0, stdout, "", False, 12)

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    result = await adapter().execute(ctx(git_repo, tmp_path))
    assert result.status == "completed"
    assert result.summary == "ok"
    # usage суммируется по ходам: ok.ndjson (7228, $0.0014528) + отчётный ход (10, $0.01).
    assert result.usage.input_tokens == 7238
    assert result.usage.cost_usd == pytest.approx(0.0114528)
    assert result.meta["session_id"] == SESSION_ID


@pytest.mark.asyncio
async def test_omp_x5_runs_in_x5_profile_with_corporate_ca(git_repo, tmp_path):
    cap = tmp_path / "cap"
    env_x5 = {"PATH": os.environ["PATH"], "FAKE_CAPTURE": str(cap), "OMP_FIXTURE": "x5_ok.ndjson"}
    result = await adapter(corporate=True, command=FAKE_ENV).execute(
        ctx(git_repo, tmp_path, env=env_x5)
    )
    # Отчётного блока в PONG нет, argv/env-тест проверяет профиль и CA.
    assert result.status == "partial"
    args = captured_argv(tmp_path)
    assert args[args.index("--profile") + 1] == "x5"
    env = captured_env(tmp_path)
    pem = adapter(corporate=True).x5_root_pem
    assert env["NODE_EXTRA_CA_CERTS"] == pem


@pytest.mark.asyncio
async def test_omp_non_corporate_has_no_profile_and_no_ca(git_repo, tmp_path):
    cap = tmp_path / "cap"
    await adapter(command=FAKE_ENV).execute(
        ctx(git_repo, tmp_path, env={"PATH": os.environ["PATH"], "FAKE_CAPTURE": str(cap)})
    )
    assert "--profile" not in captured_argv(tmp_path)
    assert "NODE_EXTRA_CA_CERTS" not in captured_env(tmp_path)


@pytest.mark.asyncio
async def test_omp_child_env_has_no_lb_key_and_no_settings_secrets(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LB_API_KEY", "leak-me")
    monkeypatch.setenv("TOP_SECRET_TOKEN", "leak-me-too")
    settings = Settings(
        secrets={"TOP_SECRET_TOKEN": SecretStr("leak-me-too")},
        executors={
            "omp/x5": ExecutorSettings(
                adapter="omp",
                command=str(FAKE_ENV),
                model="copilot-x5/x5-airun-code-large",
                corporate=True,
            )
        },
        routing=RoutingSettings(fallback_executor="omp/x5"),
    )
    cap = tmp_path / "cap"
    context = ctx(
        git_repo,
        tmp_path,
        env=child_env(settings, {"FAKE_CAPTURE": str(cap), "OMP_FIXTURE": "x5_ok.ndjson"}),
    )
    result = await build_adapters(settings)["omp/x5"].execute(context)
    assert result.status == "partial"
    env = captured_env(tmp_path)
    assert "AGENT_LB_API_KEY" not in env
    assert "TOP_SECRET_TOKEN" not in env
    assert "PATH" in env


def test_omp_adapter_requires_model():
    # Проверка в конфиге срабатывает раньше: pydantic отвергает исполнителя omp
    # без модели ещё на валидации.
    with pytest.raises(ValueError, match="model is required for adapter omp"):
        OmpAdapter("omp", ExecutorSettings(adapter="omp", command="omp", model=None), data_dir=".")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "error_head"),
    [
        ("badkey.ndjson", "Invalid API key"),
        ("down.ndjson", "Couldn't connect to 127.0.0.1:9 (connection refused)"),
    ],
)
async def test_omp_failed_turn_reports_the_provider_error(git_repo, tmp_path, fixture, error_head):
    context = ctx(git_repo, tmp_path, env={"PATH": os.environ["PATH"], "OMP_FIXTURE": fixture})
    result = await adapter().execute(context)
    assert result.status == "failed"
    assert result.error.startswith(error_head)


@pytest.mark.asyncio
async def test_omp_maxcost_is_a_budget_stop_not_an_executor_failure(git_repo, tmp_path):
    context = ctx(
        git_repo, tmp_path, env={"PATH": os.environ["PATH"], "OMP_FIXTURE": "maxcost.ndjson"}
    )
    result = await adapter().execute(context)
    # Это не сбой исполнителя: остывания нет, статус по правилам проекта для бюджета.
    assert classify_failure(result) is None
    assert result.status == "partial"
    assert result.meta["budget"] == {"reason": "omp_max_cost"}
    assert "--max-cost" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["empty", "truncated"])
async def test_omp_run_without_model_response_fails_the_executor(git_repo, tmp_path, fixture):
    context = ctx(git_repo, tmp_path, env={"PATH": os.environ["PATH"], "OMP_FIXTURE": fixture})
    result = await adapter().execute(context)
    assert result.status == "failed"
    assert result.error.startswith("omp: no model response")
    # Ход без единого ответа модели повторится на следующей задаче: остывание.
    assert classify_failure(result) == "network"


@pytest.mark.asyncio
async def test_omp_resume_continues_the_session(git_repo, tmp_path):
    cap = tmp_path / "cap"
    env_ok = {"PATH": os.environ["PATH"], "FAKE_CAPTURE": str(cap), "OMP_FIXTURE": "resume.ndjson"}
    context = ctx(git_repo, tmp_path, env=env_ok, resume_session=SESSION_ID)
    result = await adapter().execute(context)
    assert result.status == "partial"
    args = captured_argv(tmp_path)
    assert args[args.index("--resume") + 1] == SESSION_ID
    # follow-up продолжается в том же cwd, session-dir и профиле: id сессии тот же.
    assert result.meta["session_id"] == SESSION_ID


@pytest.mark.asyncio
async def test_omp_review_is_read_only(git_repo, tmp_path):
    cap = tmp_path / "cap"
    env_ro = {
        "PATH": os.environ["PATH"],
        "FAKE_CAPTURE": str(cap),
        "OMP_FIXTURE": "readonly.ndjson",
    }
    context = ctx(git_repo, tmp_path, env=env_ro, read_only=True)
    result = await adapter().execute(context)
    assert result.status == "partial"
    args = captured_argv(tmp_path)
    assert args[args.index("--tools") + 1] == "read,grep,glob"
    # Инструмента `find` у omp нет: перечисление с ним даёт код 2.
    assert "find" not in args[args.index("--tools") + 1]
    assert "--approval-mode" not in args


@pytest.mark.asyncio
async def test_omp_extra_args_override_the_default_thinking(git_repo, tmp_path):
    cap = tmp_path / "cap"
    context = ctx(git_repo, tmp_path, env={"PATH": os.environ["PATH"], "FAKE_CAPTURE": str(cap)})
    result = await adapter(extra_args=["--thinking", "high"]).execute(context)
    assert result.status == "partial"
    args = captured_argv(tmp_path)
    assert args.count("--thinking") == 1
    assert args[args.index("--thinking") + 1] == "high"


@pytest.mark.asyncio
async def test_omp_timeout_records_seconds_since_the_last_output(git_repo, tmp_path, monkeypatch):
    from agent_dispatch.executors import omp as module

    async def fake_run_cli(*args, **kwargs):
        return ProcessOutcome(
            exit_code=None,
            stdout=(FIXTURES / "ok.ndjson").read_text(),
            stderr="",
            timed_out=True,
            duration_ms=1800_000,
            last_output_age_s=12.5,
        )

    monkeypatch.setattr(module, "run_cli", fake_run_cli)
    result = await adapter().execute(ctx(git_repo, tmp_path))
    assert result.error == "timeout"
    # streams_output: сторож молчания применим, возраст тишины попадает в meta.
    assert result.meta["last_output_age_s"] == 12.5


def test_omp_fixtures_match_the_session_schema():
    validator = Draft202012Validator(load_omp_session_schema())
    for path in sorted(FIXTURES.glob("*.ndjson")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            errors = list(validator.iter_errors(json.loads(line)))
            assert not errors, f"{path.name}:{number}: {errors[0].message}"


def test_omp_fake_scripts_are_executable():
    for fake in (FAKE, FAKE_ENV):
        assert os.access(fake, os.X_OK)
        assert stat.S_IMODE(fake.stat().st_mode) & stat.S_IXUSR
