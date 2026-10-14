import json
import os
import stat
import subprocess
import sys

import pytest

LAUNCHER = os.path.join(os.path.dirname(__file__), "..", "scripts", "codex-agent-lb")


def run_launcher(tmp_path, args=(), stdin="", key=None, env_text=None):
    home = tmp_path / "home"
    native = home / ".codex/packages/standalone/current/bin/codex"
    native.parent.mkdir(parents=True)
    native.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "json.dump({'key': os.environ.get('AGENT_LB_API_KEY'), "
        "'secret': os.environ.get('SECRET_ONLY'), 'args': sys.argv[1:], "
        "'stdin': sys.stdin.read()}, open(os.environ['CAPTURE'], 'w'))\n"
        "raise SystemExit(23)\n"
    )
    native.chmod(native.stat().st_mode | stat.S_IXUSR)
    if env_text is not None:
        path = home / ".config/agent-lb/env"
        path.parent.mkdir(parents=True)
        path.write_text(env_text)
    capture = tmp_path / "capture"
    env = {
        "HOME": str(home),
        "CAPTURE": str(capture),
        "SECRET_ONLY": "",
        "PATH": os.environ["PATH"],
    }
    if key is not None:
        env["AGENT_LB_API_KEY"] = key
    result = subprocess.run([LAUNCHER, *args], input=stdin, text=True, capture_output=True, env=env)
    return result, capture


def test_env_matrix_forwards_exact_process_contract(tmp_path):
    cases = [
        (
            'export AGENT_LB_API_KEY="file"\nSECRET_ONLY=do-not-leak\n',  # pragma: allowlist secret
            None,
            "file",
        ),
        ("AGENT_LB_API_KEY=''\nAGENT_LB_API_KEY=second\n", None, "second"),
        ("AGENT_LB_API_KEY=file\n", "", "file"),
        ("AGENT_LB_API_KEY=file\n", "from-parent", "from-parent"),
    ]
    for index, (env_text, parent_key, expected) in enumerate(cases):
        result, capture = run_launcher(
            tmp_path / str(index),
            ("space arg", "", "$dollar", "`tick`"),
            "input\n",
            parent_key,
            env_text,
        )
        assert result.returncode == 23
        assert json.loads(capture.read_text()) == {
            "key": expected,
            "secret": "",
            "args": ["space arg", "", "$dollar", "`tick`"],
            "stdin": "input\n",
        }


def test_loads_only_provider_key(tmp_path):
    result, capture = run_launcher(
        tmp_path,
        ("--json",),
        "input\n",
        env_text='AGENT_LB_API_KEY="file"\nSECRET_ONLY=do-not-leak\n',  # pragma: allowlist secret
    )
    assert result.returncode == 23 and json.loads(capture.read_text())["key"] == "file"


def test_preserves_supplied_key(tmp_path):
    result, capture = run_launcher(
        tmp_path, ("one", "two"), "payload", "from-parent", "AGENT_LB_API_KEY=file\n"
    )
    assert result.returncode == 23 and json.loads(capture.read_text())["key"] == "from-parent"


def test_missing_key_fails_without_leak(tmp_path):
    result, capture = run_launcher(tmp_path, env_text="SECRET_ONLY=leak-me\n")
    assert result.returncode == 78 and not capture.exists() and "leak-me" not in result.stderr


@pytest.mark.parametrize(
    "env_text",
    [
        'export AGENT_LB_API_KEY="v"\n',
        "AGENT_LB_API_KEY=v\n",
        "   AGENT_LB_API_KEY=v\n",
        "AGENT_LB_API_KEY=\nAGENT_LB_API_KEY=v\n",
        "AGENT_LB_API_KEY=\n",
        "AGENT_LB_API_KEY=''\n",
        # Wrapper ждёт ровно один пробел после `export`.
        "export   AGENT_LB_API_KEY=v\n",
        "export\tAGENT_LB_API_KEY=v\n",
        "# AGENT_LB_API_KEY=v\n",
        # `str.lstrip()` срезал бы эти символы, shell `[[:space:]]` нет.
        "\x1cAGENT_LB_API_KEY=v\n",
        "\xa0AGENT_LB_API_KEY=v\n",
    ],
)
def test_doctor_reads_the_env_file_exactly_like_the_wrapper(tmp_path, env_text):
    # doctor обещает «wrapper найдёт ключ»: его вердикт обязан совпадать с
    # настоящим скриптом, иначе зелёный doctor и `exit 78` на задаче.
    from agent_dispatch.doctor import _env_file_value

    result, _capture = run_launcher(tmp_path, env_text=env_text)
    wrapper_found = result.returncode != 78
    doctor_found = (
        _env_file_value(tmp_path / "home/.config/agent-lb/env", "AGENT_LB_API_KEY") is not None
    )

    assert doctor_found == wrapper_found


def test_doctor_reads_a_non_utf8_env_file_like_the_wrapper(tmp_path):
    # Битый UTF-8 в комментарии wrapper не мешает; doctor не должен падать.
    from agent_dispatch.doctor import _env_file_value

    env_file = tmp_path / "home/.config/agent-lb/env"
    env_file.parent.mkdir(parents=True)
    env_file.write_bytes(b"# \xff\xfe legacy\nAGENT_LB_API_KEY=v\n")

    result, _capture = run_launcher(tmp_path)

    assert result.returncode != 78
    assert _env_file_value(env_file, "AGENT_LB_API_KEY") == "v"
