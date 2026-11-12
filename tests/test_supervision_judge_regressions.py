from __future__ import annotations

import json
import sys

import pytest

from agent_dispatch.config import Settings
from agent_dispatch.dispatch.supervision import SYSTEM_PROMPT, JudgmentError, judge


@pytest.fixture
def supervisor_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = tmp_path / ".omp" / "profiles" / "te-sidecall" / "agent"
    profile.mkdir(parents=True)
    (profile / "models.yml").write_text("{}")
    capture = tmp_path / "capture.json"
    command = tmp_path / "supervisor-cli"
    command.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, pathlib, sys
payload = json.load(sys.stdin)
pathlib.Path(os.environ["JUDGE_CAPTURE"]).write_text(json.dumps({
    "payload": payload, "args": sys.argv[1:], "cwd": os.getcwd(),
    "overlay": pathlib.Path(sys.argv[sys.argv.index("--config") + 1]).read_text(),
}))
case = json.loads(os.environ["JUDGE_CASE"])
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant", "provider": "agent-lb", "model": "gpt-6.1-sol",
    "stopReason": case.get("stop", "stop"), "errorMessage": case.get("message"),
    "usage": {"input": 42}, "content": [{"type": "text", "text": json.dumps({
        "decision": "continue", "reason": "progress observed", "evidence": []
    })}]
}}))
print(case.get("stderr", ""), file=sys.stderr)
sys.exit(case.get("exit", 0))
"""
    )
    command.chmod(0o755)
    monkeypatch.setenv("JUDGE_CAPTURE", str(capture))
    monkeypatch.setenv("JUDGE_CASE", "{}")
    settings = Settings.model_validate(
        {
            "routing": {"fallback_executor": "worker"},
            "executors": {"worker": {"adapter": "omp", "model": "agent-lb/gpt-6.1-sol"}},
            "execution": {"supervision": {"command": str(command), "evidence_max_bytes": 131072}},
            "secrets": {"EXAMPLE_KEY": "configured-supervisor-secret"},
        }
    )
    return settings, capture


@pytest.mark.asyncio
async def test_large_unicode_mandate_and_evidence_reach_isolated_cli(supervisor_cli, tmp_path):
    settings, capture = supervisor_cli
    payload = {
        "immutable_mandate": {"task": "🔬" * 10000, "constraints": ["no changes"]},
        "untrusted_evidence": {"new_log": "Ж" * 65536, "current_artifact_diff": {"text": "x"}},
    }
    assert len(json.dumps(payload).encode()) > 131072
    verdict, metrics = await judge(settings, payload, tmp_path / "judge.log")
    received = json.loads(capture.read_text())
    assert received["payload"] == payload
    assert verdict.decision == "continue"
    assert metrics["input"] == 42
    args = received["args"]
    assert args[args.index("--system-prompt") + 1] == SYSTEM_PROMPT
    assert args[-1] == SYSTEM_PROMPT
    assert {"--no-tools", "--no-extensions", "--no-skills", "--no-rules", "--no-session"} <= set(
        args
    )
    assert received["cwd"] != str(tmp_path)
    assert "modelFallback: false" in received["overlay"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 9])
async def test_provider_error_wins_over_stderr_and_is_redacted(
    supervisor_cli, tmp_path, monkeypatch, exit_code
):
    settings, _ = supervisor_cli
    monkeypatch.setenv(
        "JUDGE_CASE",
        json.dumps(
            {
                "stop": "error",
                "message": "provider rejected configured-supervisor-secret; Bearer abc123-secret",
                "stderr": "irrelevant stderr diagnostic",
                "exit": exit_code,
            }
        ),
    )
    with pytest.raises(JudgmentError) as error:
        await judge(settings, {"immutable_mandate": {"task": "check"}}, tmp_path / "judge.log")
    reason = str(error.value)
    assert "provider rejected" in reason
    assert "[REDACTED]" in reason
    assert "configured-supervisor-secret" not in reason
    assert "abc123-secret" not in reason
    assert "irrelevant stderr diagnostic" not in reason
    assert error.value.metrics["input"] == 42


@pytest.mark.asyncio
async def test_provider_error_without_message_uses_redacted_stderr(
    supervisor_cli, tmp_path, monkeypatch
):
    settings, _ = supervisor_cli
    monkeypatch.setenv(
        "JUDGE_CASE",
        json.dumps({"stop": "error", "stderr": "failed: configured-supervisor-secret"}),
    )
    with pytest.raises(JudgmentError) as error:
        await judge(settings, {"immutable_mandate": {"task": "check"}}, tmp_path / "judge.log")
    assert "failed: [REDACTED]" in str(error.value)
    assert "configured-supervisor-secret" not in str(error.value)
