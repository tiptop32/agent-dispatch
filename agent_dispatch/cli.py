from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable
from typing import Any

import typer

from agent_dispatch import doctor as doctor_module
from agent_dispatch import serve_state
from agent_dispatch.config import Settings, load_settings
from agent_dispatch.mcp import autostart
from agent_dispatch.mcp import server as mcp_server
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.models import DispatchRequest, SourceAgent, TaskStatus, TaskView
from agent_dispatch.server import run_server

app = typer.Typer(
    name="agent-dispatch",
    help=(
        "AgentDispatch: route coding tasks to Claude Code, Codex or OpenCode. "
        "Start anywhere. Dispatch anywhere."
    ),
    no_args_is_help=True,
)
_FINAL_STATUSES = {
    TaskStatus.completed,
    TaskStatus.partial,
    TaskStatus.failed,
    TaskStatus.needs_context,
    TaskStatus.needs_escalation,
    TaskStatus.cancelled,
}


def _daemon_error(detail: str | None = None) -> None:
    message = "daemon is not running: start with `agent-dispatch serve`"
    if detail:
        message = f"{message} ({detail})"
    typer.echo(message, err=True)
    raise typer.Exit(2)


async def _open_client(settings: Settings, wait: int, start: bool) -> DispatchClient:
    try:
        if start:
            state = await autostart.ensure_daemon(settings)
        else:
            state = serve_state.read_state(settings.server.data_dir)
            if state is None or not serve_state.is_alive(state):
                _daemon_error()
    except DaemonUnavailable as exc:
        _daemon_error(str(exc))
    return DispatchClient(state, timeout=wait + 30)


def _client(settings: Settings, wait: int) -> DispatchClient:
    """Return a client for an already running daemon."""
    return asyncio.run(_open_client(settings, wait, False))


async def _call(
    settings: Settings,
    wait: int,
    operation: str,
    *args: object,
    start: bool = False,
) -> Any:
    client = await _open_client(settings, wait, start)
    try:
        async with client:
            return await getattr(client, operation)(*args)
    except DaemonUnavailable as exc:
        _daemon_error(str(exc))


async def _dispatch_and_wait(
    settings: Settings,
    request: DispatchRequest,
    wait: int,
    poll_seconds: float,
) -> list[TaskView]:
    started = time.monotonic()
    client = await _open_client(settings, wait, True)
    views: list[TaskView] = []
    seen: set[str] = set()
    async with client:
        view = await client.submit(request)
        while True:
            if view.status in _FINAL_STATUSES:
                views.append(view)
                seen.add(view.task_id)
                escalated_to = view.result.meta.get("escalated_to") if view.result else None
                if not escalated_to or escalated_to in seen:
                    break
                if not _within_wait(started, wait):
                    break
                if not isinstance(escalated_to, str):
                    break
                view = await client.status(escalated_to)
                continue
            if not _within_wait(started, wait):
                views.append(view)
                break
            if poll_seconds:
                remaining = wait - (time.monotonic() - started)
                await asyncio.sleep(min(poll_seconds, max(0.0, remaining)))
            view = await client.status(view.task_id)
    return views


def _within_wait(started: float, wait: int) -> bool:
    return wait > 0 and time.monotonic() - started < wait


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def _request(
    task: str,
    cwd: str,
    context: str | None,
    files: list[str] | None,
    constraints: list[str] | None,
    *,
    executor: str | None = None,
    wait: int = 1800,
    timeout: int | None = None,
    allow_escalation: bool = True,
) -> DispatchRequest:
    return DispatchRequest(
        task=task,
        cwd=cwd,
        context=context,
        files=files or [],
        constraints=constraints or [],
        source_agent=SourceAgent.cli,
        executor=executor,
        wait_seconds=wait,
        timeout_seconds=timeout,
        allow_escalation=allow_escalation,
    )


def _print_task(view: TaskView, as_json: bool) -> None:
    if as_json:
        typer.echo(view.model_dump_json(indent=2))
        return
    result = view.result
    executor = (
        result.executor
        if result
        else view.decision.executor
        if view.decision
        else view.request.executor or "-"
    )
    typer.echo(f"task_id: {view.task_id}")
    typer.echo(f"status: {view.status}")
    typer.echo(f"executor: {executor}")
    typer.echo(f"changed_files: {','.join(result.changed_files) if result else ''}")
    typer.echo(f"summary: {result.summary if result else ''}")
    if view.status not in _FINAL_STATUSES:
        typer.echo(f"still running: agent-dispatch status {view.task_id}")


@app.command(help="Run the AgentDispatch daemon.")
def serve() -> None:
    run_server(load_settings())


@app.command("mcp", help="Run the MCP server.")
def mcp_command() -> None:
    mcp_server.main()


@app.command(help="Route a coding task without executing it.")
def route(
    task: str,
    cwd: str = typer.Option("."),
    context: str | None = typer.Option(None),
    files: list[str] | None = typer.Option(None, "--file"),  # noqa: B008
    constraints: list[str] | None = typer.Option(None, "--constraint"),  # noqa: B008
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    settings = load_settings()
    decision = _run(
        _call(
            settings,
            settings.mcp.wait_seconds,
            "route",
            _request(task, cwd, context, files, constraints),
            start=True,
        )
    )
    if as_json:
        typer.echo(decision.model_dump_json(indent=2))
        return
    typer.echo(f"executor: {decision.executor}")
    typer.echo(f"confidence: {decision.confidence:.2f}")
    typer.echo(f"router: {decision.router}")
    typer.echo(f"reason: {decision.reason or '-'}")


@app.command(help="Dispatch a coding task to an executor.")
def dispatch(
    task: str,
    cwd: str = typer.Option("."),
    context: str | None = typer.Option(None),
    files: list[str] | None = typer.Option(None, "--file"),  # noqa: B008
    constraints: list[str] | None = typer.Option(None, "--constraint"),  # noqa: B008
    executor: str | None = typer.Option(None),
    wait: int = typer.Option(1800, min=0),
    timeout: int | None = typer.Option(None, min=1),
    poll_seconds: float = typer.Option(2.0, "--poll", min=0.0, help="Polling interval in seconds."),
    no_escalation: bool = typer.Option(False, "--no-escalation"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    settings = load_settings()
    views = _run(
        _dispatch_and_wait(
            settings,
            _request(
                task,
                cwd,
                context,
                files,
                constraints,
                executor=executor,
                wait=wait,
                timeout=timeout,
                allow_escalation=not no_escalation,
            ),
            wait,
            poll_seconds,
        )
    )
    if as_json:
        _print_task(views[-1], True)
    else:
        for index, view in enumerate(views):
            if index:
                typer.echo(f"escalated to {view.task_id}")
            _print_task(view, False)


@app.command(help="Show the status of a task.")
def status(task_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    settings = load_settings()
    _print_task(_run(_call(settings, settings.mcp.wait_seconds, "status", task_id)), as_json)


@app.command(help="Cancel a task.")
def cancel(task_id: str) -> None:
    settings = load_settings()
    _print_task(_run(_call(settings, settings.mcp.wait_seconds, "cancel", task_id)), False)


@app.command("executors", help="List configured executors.")
def list_executors() -> None:
    settings = load_settings()
    rows = _run(_call(settings, settings.mcp.wait_seconds, "executors"))
    typer.echo("name\tadapter\tmodel\tenabled\tavailable\tversion/error")
    for row in rows:
        version_or_error = row.get("version") or row.get("error") or "-"
        typer.echo(
            f"{row['name']}\t{row['adapter']}\t{row.get('model') or '-'}\t"
            f"{row['enabled']}\t{row.get('available')}\t{version_or_error}"
        )


@app.command(help="Record feedback for a task.")
def feedback(
    task_id: str,
    outcome: str = typer.Option(...),
    note: str | None = typer.Option(None),
) -> None:
    settings = load_settings()
    response = _run(_call(settings, settings.mcp.wait_seconds, "feedback", task_id, outcome, note))
    typer.echo(json.dumps(response))


@app.command("export", help="Export task records as JSONL.")
def export_data(
    since: str = typer.Option("7d"),
    output_format: str = typer.Option("jsonl", "--format"),
) -> None:
    if output_format != "jsonl":
        raise typer.BadParameter("only jsonl is supported", param_hint="--format")
    settings = load_settings()
    typer.echo(_run(_call(settings, settings.mcp.wait_seconds, "export", since)), nl=False)


@app.command(help="Run local and optional online health checks.")
def doctor(
    online: bool = typer.Option(False, "--online"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    checks = _run(doctor_module.run_checks(load_settings(), online))
    if as_json:
        typer.echo(json.dumps([check.model_dump() for check in checks]))
    else:
        typer.echo(doctor_module.format_checks(checks))
    if any(not check.ok for check in checks):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
