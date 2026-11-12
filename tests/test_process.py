import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from agent_dispatch.executors.process import run_cli

FAKES_DIR = Path(__file__).parent / "fakes"


async def test_run_cli_collects_stdout_on_exit_zero(tmp_path: Path) -> None:
    outcome = await run_cli(
        [str(FAKES_DIR / "echo_ok.sh")],
        cwd=tmp_path,
        env={},
        stdin="hello",
        timeout_seconds=1,
        log_path=tmp_path / "process.log",
    )

    assert outcome.exit_code == 0
    assert outcome.stdout == "IN:hello\n"
    assert outcome.timed_out is False


async def test_run_cli_returns_nonzero_exit_code(tmp_path: Path) -> None:
    outcome = await run_cli(
        [str(FAKES_DIR / "exit_3.sh")],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=1,
        log_path=tmp_path / "process.log",
    )

    assert outcome.exit_code == 3


async def test_run_cli_idle_watchdog_stalls_silent_process(tmp_path: Path) -> None:
    outcome = await run_cli(
        [sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdin=None,
        timeout_seconds=10,
        idle_timeout_seconds=0.5,
        grace_seconds=0.1,
        log_path=tmp_path / "process.log",
    )
    assert outcome.stalled is True
    assert outcome.timed_out is False
    assert outcome.exit_code is None
    assert outcome.last_output_age_s is not None
    assert outcome.duration_ms < 5000


async def test_run_cli_idle_watchdog_allows_regular_output(tmp_path: Path) -> None:
    outcome = await run_cli(
        [
            sys.executable,
            "-c",
            "import time; [print(i, flush=True) or time.sleep(.2) for i in range(8)]",
        ],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdin=None,
        timeout_seconds=10,
        idle_timeout_seconds=0.5,
        log_path=tmp_path / "process.log",
    )
    assert outcome.stalled is False
    assert outcome.exit_code == 0


async def test_run_cli_timeout_marks_process_timed_out(tmp_path: Path) -> None:
    outcome = await run_cli(
        [str(FAKES_DIR / "sleep_forever.sh")],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=0.2,
        grace_seconds=0.2,
        log_path=tmp_path / "process.log",
    )

    assert outcome.timed_out is True
    assert outcome.exit_code is None


async def test_run_cli_kills_process_ignoring_term(tmp_path: Path) -> None:
    started_at = time.monotonic()
    outcome = await run_cli(
        [str(FAKES_DIR / "ignore_term.sh")],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=0.2,
        grace_seconds=0.2,
        log_path=tmp_path / "process.log",
    )

    assert outcome.timed_out is True
    assert time.monotonic() - started_at < 1


async def test_run_cli_kills_entire_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(
        run_cli(
            [str(FAKES_DIR / "spawn_child_and_sleep.sh")],
            cwd=tmp_path,
            env={"FAKE_PID_FILE": str(pid_file)},
            stdin=None,
            timeout_seconds=1.0,
            grace_seconds=0.2,
            log_path=tmp_path / "process.log",
        )
    )
    deadline = time.monotonic() + 1
    while not pid_file.exists() and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    outcome = await task
    assert outcome.timed_out is True
    pgid = int(pid_file.read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("process group is still alive")


async def test_run_cli_cancellation_kills_group_and_reraises(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(
        run_cli(
            [str(FAKES_DIR / "spawn_child_and_sleep.sh")],
            cwd=tmp_path,
            env={"FAKE_PID_FILE": str(pid_file)},
            stdin=None,
            timeout_seconds=10,
            grace_seconds=0.2,
            log_path=tmp_path / "process.log",
        )
    )
    deadline = time.monotonic() + 1
    while not pid_file.exists() and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation was swallowed")
    pgid = int(pid_file.read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("cancelled process group is still alive")


async def test_run_cli_writes_stdin(tmp_path: Path) -> None:
    outcome = await run_cli(
        [str(FAKES_DIR / "echo_ok.sh")],
        cwd=tmp_path,
        env={},
        stdin="payload",
        timeout_seconds=1,
        log_path=tmp_path / "process.log",
    )

    assert outcome.stdout == "IN:payload\n"


async def test_run_cli_logs_stdout_and_prefixed_stderr(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "process.log"
    await run_cli(
        [str(FAKES_DIR / "echo_ok.sh")],
        cwd=tmp_path,
        env={},
        stdin="payload",
        timeout_seconds=1,
        log_path=log_path,
    )

    assert log_path.read_text() == "IN:payload\n[stderr] warn\n"


async def test_run_cli_appends_to_existing_log(tmp_path: Path) -> None:
    log_path = tmp_path / "process.log"
    log_path.write_text("previous\n")

    await run_cli(
        [str(FAKES_DIR / "exit_3.sh")],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=1,
        log_path=log_path,
    )

    assert log_path.read_text() == "previous\n"


async def test_run_cli_reports_nonnegative_duration(tmp_path: Path) -> None:
    outcome = await run_cli(
        [str(FAKES_DIR / "exit_3.sh")],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=1,
        log_path=tmp_path / "process.log",
    )

    assert outcome.duration_ms >= 0


async def test_run_cli_handles_large_stdin_without_deadlock(tmp_path: Path) -> None:
    payload = "x" * 300_000
    outcome = await run_cli(
        [str(FAKES_DIR / "echo_big.sh")],
        cwd=tmp_path,
        env={},
        stdin=payload,
        timeout_seconds=5,
        log_path=tmp_path / "process.log",
    )
    assert outcome.stdout == payload


async def test_run_cli_cancellation_while_writing_stdin_kills_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(
        run_cli(
            [str(FAKES_DIR / "sleep_no_read.sh")],
            cwd=tmp_path,
            env={"FAKE_PID_FILE": str(pid_file)},
            stdin="x" * 300_000,
            timeout_seconds=10,
            grace_seconds=0.2,
            log_path=tmp_path / "process.log",
        )
    )
    deadline = time.monotonic() + 1
    while not pid_file.exists() and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation was swallowed")
    pgid = int(pid_file.read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("cancelled process group is still alive")


async def test_run_cli_handles_long_line_without_newline(tmp_path: Path) -> None:
    outcome = await run_cli(
        [str(FAKES_DIR / "long_line.sh")],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=1,
        log_path=tmp_path / "process.log",
    )
    assert outcome.stdout == "x" * 200_000
    assert outcome.exit_code == 0


async def test_run_cli_kills_child_after_parent_exits_on_term(tmp_path: Path) -> None:
    # Таймаут 1.0 с, а не 0.3: старт /bin/sh под нагрузкой может занять сотни мс,
    # и группа была бы убита до записи pid-файла (флаки).
    started_at = time.monotonic()
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(
        run_cli(
            [str(FAKES_DIR / "parent_exits_child_ignores_term.sh")],
            cwd=tmp_path,
            env={"FAKE_PID_FILE": str(pid_file)},
            stdin=None,
            timeout_seconds=1.0,
            grace_seconds=0.2,
            log_path=tmp_path / "process.log",
        )
    )
    deadline = time.monotonic() + 1
    while not pid_file.exists() and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    outcome = await task
    assert outcome.timed_out is True
    assert time.monotonic() - started_at < 2.5
    pgid = int(pid_file.read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


async def test_run_cli_handles_process_that_exits_immediately(tmp_path: Path) -> None:
    # Быстрый процесс может завершиться до того, как run_cli спросит pgid:
    # os.getpgid на уже собранном pid бросал ProcessLookupError.
    outcome = await run_cli(
        ["/usr/bin/true"],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=1,
        log_path=tmp_path / "process.log",
    )
    assert outcome.exit_code == 0
    assert outcome.timed_out is False


async def test_run_cli_timeout_records_seconds_since_the_last_output(tmp_path: Path) -> None:
    # Печатает и замолкает: к убийству молчит около секунды, а не весь запуск.
    outcome = await run_cli(
        [sys.executable, "-c", "import time; print('hi', flush=True); time.sleep(30)"],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=1.0,
        grace_seconds=0.2,
        log_path=tmp_path / "process.log",
    )

    assert outcome.timed_out is True
    assert outcome.last_output_age_s is not None
    assert 0.3 <= outcome.last_output_age_s <= 1.5


async def test_run_cli_without_timeout_has_no_output_age(tmp_path: Path) -> None:
    outcome = await run_cli(
        [sys.executable, "-c", "print('done')"],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=10,
        log_path=tmp_path / "process.log",
    )

    assert outcome.timed_out is False
    assert outcome.last_output_age_s is None


async def test_run_cli_delivers_complete_utf8_stdout_lines(tmp_path: Path) -> None:
    lines = []
    outcome = await run_cli(
        [
            sys.executable,
            "-c",
            "import os,time; "
            "os.write(1,b'first\\n\\xd0'); time.sleep(0.1); "
            "os.write(2,b'not a stdout line\\n'); "
            "os.write(1,b'\\xbf\\xd1\\x80'); time.sleep(0.1); "
            "os.write(1,b'\\xd0\\xb8\\xd0\\xb2\\xd0\\xb5\\xd1\\x82\\r\\nlast')",
        ],
        cwd=tmp_path,
        env={},
        stdin=None,
        timeout_seconds=5,
        log_path=tmp_path / "lines.log",
        on_line=lines.append,
    )
    assert outcome.exit_code == 0
    assert lines == ["first", "привет", "last"]
    assert outcome.stdout == "first\nпривет\r\nlast"
    assert (tmp_path / "lines.log").read_text().count("привет") == 1


async def test_cancellation_during_process_creation_does_not_lose_the_child(tmp_path, monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(
        run_cli(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=dict(os.environ),
            stdin=None,
            timeout_seconds=10,
            log_path=tmp_path / "launch.log",
        )
    )
    await asyncio.wait_for(created.wait(), 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


async def test_normal_cli_exit_does_not_leave_a_background_writer(tmp_path):
    marker = tmp_path / "late-write"
    child = (
        "import pathlib,time,sys;time.sleep(0.6);pathlib.Path(sys.argv[1]).write_text('escaped')"
    )
    parent = (
        "import subprocess,sys;subprocess.Popen("
        f"[sys.executable,'-c',{child!r},{str(marker)!r}],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )
    outcome = await run_cli(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        env=dict(os.environ),
        stdin=None,
        timeout_seconds=5,
        log_path=tmp_path / "exit.log",
        grace_seconds=0.2,
    )
    assert outcome.exit_code == 0
    await asyncio.sleep(0.7)
    assert not marker.exists()
