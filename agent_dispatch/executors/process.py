import asyncio
import codecs
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_dispatch.executors.watchdog import OmpWatchdog


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    stalled: bool = False
    idle_seconds: float | None = None
    #: Сколько секунд CLI молчал (stdout и stderr) к моменту жёсткого таймаута.
    #: Отличает агента, работавшего до конца, от зависшего. None без таймаута.
    last_output_age_s: float | None = None
    #: Configured hard timeout, retained for machine-readable diagnostics.
    timeout_seconds: float | None = None
    watchdog: dict | None = None


async def run_cli(
    argv: list[str],
    *,
    cwd: str | Path,
    env: dict[str, str],
    stdin: str | None,
    timeout_seconds: float,
    log_path: Path,
    grace_seconds: float = 10.0,
    idle_timeout_seconds: float | None = None,
    on_line: Callable[[str], None] | None = None,
    watchdog: OmpWatchdog | None = None,
    watchdog_interval_seconds: float = 300,
) -> ProcessOutcome:
    """Run a CLI process, streaming output and terminating its process group."""
    started_at = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    # start_new_session=True делает процесс лидером новой группы: pgid == pid.
    # os.getpgid(pid) здесь гонялся бы с child watcher: быстрый процесс уже собран,
    # и вызов бросает ProcessLookupError.
    pgid = process.pid

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    last_output_at = time.monotonic()

    async def read_stream(stream: asyncio.StreamReader, chunks: list[bytes], prefix: str) -> None:
        nonlocal last_output_at
        with log_path.open("a") as log_file:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            pending = ""
            while data := await stream.read(65536):
                last_output_at = time.monotonic()
                chunks.append(data)
                text = decoder.decode(data)
                log_file.write(f"{prefix}{text}")
                log_file.flush()
                if on_line is not None and not prefix:
                    pending += text
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        on_line(line.removesuffix("\r"))
            final = decoder.decode(b"", final=True)
            if final:
                log_file.write(final)
            if on_line is not None and not prefix and pending + final:
                on_line((pending + final).removesuffix("\r"))

    readers = [
        asyncio.create_task(read_stream(process.stdout, stdout_chunks, "")),
        asyncio.create_task(read_stream(process.stderr, stderr_chunks, "[stderr] ")),
    ]

    async def write_stdin() -> None:
        if stdin is None:
            return
        try:
            process.stdin.write(stdin.encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            process.stdin.close()

    writer = asyncio.create_task(write_stdin())

    def signal_group(sig: signal.Signals) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    async def terminate_group() -> None:
        signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            pass
        finally:
            signal_group(signal.SIGKILL)
        if process.returncode is None:
            await process.wait()

    async def wait_for_io() -> None:
        tasks = [*readers, writer]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=grace_seconds)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    timed_out = False
    stalled = False
    last_output_age_s: float | None = None
    watchdog_result = None
    next_watchdog_at = time.monotonic() + watchdog_interval_seconds
    idle_limit = idle_timeout_seconds if idle_timeout_seconds and idle_timeout_seconds > 0 else None
    if watchdog is not None:
        idle_limit = None
    process_wait = asyncio.create_task(process.wait())
    try:
        deadline = time.monotonic() + timeout_seconds
        while not process_wait.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                # До SIGTERM: вывод при завершении не должен сбросить счётчик.
                last_output_age_s = round(time.monotonic() - last_output_at, 1)
                await terminate_group()
                break
            poll = min(remaining, 1.0)
            if watchdog is not None:
                poll = min(poll, max(next_watchdog_at - time.monotonic(), 0.001))
            if idle_limit is not None:
                poll = min(poll, max(idle_limit / 4, 0.001))
            try:
                await asyncio.wait_for(asyncio.shield(process_wait), timeout=poll)
            except TimeoutError:
                if watchdog is not None and time.monotonic() >= next_watchdog_at:
                    next_watchdog_at = time.monotonic() + watchdog_interval_seconds
                    watchdog_result = watchdog.check()
                    if watchdog_result is not None:
                        idle_limit = (
                            watchdog.tool_idle_seconds
                            if watchdog_result["tool_open"]
                            else watchdog.idle_seconds
                        )
                        stalled = True
                        last_output_age_s = round(time.monotonic() - watchdog.last_activity_at, 1)
                        await terminate_group()
                        break
                if idle_limit is not None and time.monotonic() - last_output_at >= idle_limit:
                    stalled = True
                    last_output_age_s = round(time.monotonic() - last_output_at, 1)
                    await terminate_group()
                    break
        await wait_for_io()
    except asyncio.CancelledError:
        await terminate_group()
        await wait_for_io()
        raise
    finally:
        if process.returncode is None:
            await terminate_group()
        if not process_wait.done():
            process_wait.cancel()
            await asyncio.gather(process_wait, return_exceptions=True)

    stdout = b"".join(stdout_chunks).decode(errors="replace")
    stderr = b"".join(stderr_chunks).decode(errors="replace")
    return ProcessOutcome(
        exit_code=None if timed_out or stalled else process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        stalled=stalled,
        idle_seconds=idle_limit,
        last_output_age_s=last_output_age_s,
        timeout_seconds=timeout_seconds,
        watchdog=watchdog_result,
    )
