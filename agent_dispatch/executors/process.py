import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    stalled: bool = False
    idle_seconds: float | None = None


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
            while data := await stream.read(65536):
                last_output_at = time.monotonic()
                chunks.append(data)
                text = data.decode(errors="replace")
                log_file.write(f"{prefix}{text}")
                log_file.flush()

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
    idle_limit = idle_timeout_seconds if idle_timeout_seconds and idle_timeout_seconds > 0 else None
    process_wait = asyncio.create_task(process.wait())
    try:
        deadline = time.monotonic() + timeout_seconds
        while not process_wait.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                await terminate_group()
                break
            poll = min(remaining, 1.0)
            if idle_limit is not None:
                poll = min(poll, max(idle_limit / 4, 0.001))
            try:
                await asyncio.wait_for(asyncio.shield(process_wait), timeout=poll)
            except TimeoutError:
                if idle_limit is not None and time.monotonic() - last_output_at >= idle_limit:
                    stalled = True
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
    )
