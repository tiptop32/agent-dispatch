from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import typer

from agent_dispatch import doctor as doctor_module
from agent_dispatch import serve_state
from agent_dispatch.config import Settings, load_settings
from agent_dispatch.executors import worktree
from agent_dispatch.mcp import autostart
from agent_dispatch.mcp import server as mcp_server
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.models import FINAL_STATUSES, DispatchRequest, SourceAgent, TaskView
from agent_dispatch.server import run_server

app = typer.Typer(
    name="agent-dispatch",
    help=(
        "AgentDispatch: route coding tasks to Claude Code, Codex or OpenCode. "
        "Start anywhere. Dispatch anywhere."
    ),
    no_args_is_help=True,
)


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
            if state is None or not serve_state.is_running(state):
                _daemon_error()
    except DaemonUnavailable as exc:
        _daemon_error(str(exc))
    return DispatchClient(state, timeout=wait + 30)


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


def _daemon_call(operation: str, *args: object, start: bool = False) -> Any:
    """Одно обращение к демону: настройки, клиент, вызов операции."""
    settings = load_settings()
    return asyncio.run(_call(settings, settings.mcp.wait_seconds, operation, *args, start=start))


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
            if view.status in FINAL_STATUSES:
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
    if view.status not in FINAL_STATUSES:
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
    decision = _daemon_call("route", _request(task, cwd, context, files, constraints), start=True)
    if as_json:
        typer.echo(decision.model_dump_json(indent=2))
        return
    typer.echo(f"executor: {decision.executor}")
    if decision.capability:
        typer.echo(f"capability: {decision.capability}")
    typer.echo(f"confidence: {decision.confidence:.2f} ({decision.confidence_tier or '-'})")
    typer.echo(f"router: {decision.router}")
    typer.echo(f"reason: {decision.reason or '-'}")
    for note in decision.meta.get("selection_notes", []):
        typer.echo(f"note: {note}")


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
    views = asyncio.run(
        _dispatch_and_wait(
            load_settings(),
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
    _print_task(_daemon_call("status", task_id), as_json)


@app.command(help="Cancel a task.")
def cancel(task_id: str) -> None:
    _print_task(_daemon_call("cancel", task_id), False)


@app.command("executors", help="List configured executors.")
def list_executors() -> None:
    rows = _daemon_call("executors")
    typer.echo("name\tadapter\tmodel\tenabled\tavailable\tversion/error")
    for row in rows:
        version_or_error = row.get("version") or row.get("error") or "-"
        typer.echo(
            f"{row['name']}\t{row['adapter']}\t{row.get('model') or '-'}\t"
            f"{row['enabled']}\t{row.get('available')}\t{version_or_error}"
        )


@app.command("worktrees", help="List or clean up AgentDispatch worktrees of a repository.")
def worktrees(
    cwd: str = typer.Option(".", help="Any path inside the repository."),
    clean: bool = typer.Option(False, "--clean", help="Remove the listed worktrees."),
    keep_branches: bool = typer.Option(
        True, "--keep-branches/--delete-branches", help="Keep branches when cleaning."
    ),
) -> None:
    settings = load_settings()
    prefix = settings.execution.branch_prefix
    try:
        trees = worktree.list_worktrees(cwd, prefix)
        # Ветки без каталога остаются от `integrate: branch`, их тоже надо показать.
        orphans = worktree.list_branches(cwd, prefix)
    except worktree.WorktreeError as error:
        raise typer.BadParameter(str(error), param_hint="--cwd") from error
    if not trees and not orphans:
        typer.echo("no agent-dispatch worktrees or branches")
        return
    for tree in trees:
        if clean:
            try:
                worktree.remove(tree, keep_branch=keep_branches)
            except worktree.WorktreeError as error:
                typer.echo(f"{tree.path}\tERROR\t{error}")
                continue
            typer.echo(f"{tree.path}\tremoved\t{tree.branch}")
        else:
            typer.echo(f"{tree.path}\t{tree.branch}")
    repo = worktree.repo_root(cwd)
    for branch in orphans:
        if clean and not keep_branches:
            try:
                worktree.delete_branch(repo, branch)
            except worktree.WorktreeError as error:
                typer.echo(f"(no worktree)\tERROR\t{error}")
                continue
            typer.echo(f"(no worktree)\tdeleted\t{branch}")
        else:
            typer.echo(f"(no worktree)\t{branch}")


@app.command(help="Record feedback for a task.")
def feedback(
    task_id: str,
    outcome: str = typer.Option(...),
    note: str | None = typer.Option(None),
) -> None:
    typer.echo(json.dumps(_daemon_call("feedback", task_id, outcome, note)))


@app.command("export", help="Export task records as JSONL.")
def export_data(
    since: str = typer.Option("7d"),
    output_format: str = typer.Option("jsonl", "--format"),
) -> None:
    if output_format != "jsonl":
        raise typer.BadParameter("only jsonl is supported", param_hint="--format")
    typer.echo(_daemon_call("export", since), nl=False)


@app.command(help="Run local and optional online health checks.")
def doctor(
    online: bool = typer.Option(False, "--online"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    checks = asyncio.run(doctor_module.run_checks(load_settings(), online))
    if as_json:
        typer.echo(json.dumps([check.model_dump() for check in checks]))
    else:
        typer.echo(doctor_module.format_checks(checks))
    if any(not check.ok for check in checks):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
