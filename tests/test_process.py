import asyncio
import os
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
