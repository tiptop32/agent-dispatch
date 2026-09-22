from __future__ import annotations

import inspect
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agent_dispatch.config import Settings, load_settings
from agent_dispatch.mcp.autostart import ensure_daemon
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.models import ContextMode, DispatchRequest, SourceAgent, TaskStatus, TaskView
from agent_dispatch.serve_state import ServeState

ClientFactory = Callable[[ServeState, float], DispatchClient]


def _agent(value: str | None, default: SourceAgent | None = None) -> SourceAgent | None:
    try:
        return SourceAgent(value) if value else default
    except ValueError:
        return default


def _request(task: str, cwd: str, settings: Settings, **kwargs: Any) -> DispatchRequest:
    source = (
        _agent(os.environ.get("AGENT_DISPATCH_SOURCE_AGENT"), SourceAgent.unknown)
        or SourceAgent.unknown
    )
    hop_raw = os.environ.get("AGENT_DISPATCH_HOP", "0")
    try:
        hop = int(hop_raw)
    except ValueError:
        hop = 0
    wait_seconds = kwargs.pop("wait_seconds", None)
    return DispatchRequest(
        task=task,
        cwd=cwd,
        source_agent=source,
        parent_task_id=os.environ.get("AGENT_DISPATCH_TASK_ID"),
        root_agent=_agent(os.environ.get("AGENT_DISPATCH_ROOT_AGENT")),
        hop=hop,
        wait_seconds=settings.mcp.wait_seconds if wait_seconds is None else wait_seconds,
        **kwargs,
    )


def _task_text(view: TaskView) -> str:
    result = view.result
    executor = result.executor if result else (view.decision.executor if view.decision else "")
    changed = ", ".join(result.changed_files) if result else ""
    summary = result.summary if result else ""
    text = (
        f"task_id: {view.task_id}\nstatus: {view.status.value}\n"
        f"executor: {executor}\nchanged_files: {changed}\nsummary: {summary}\n\n"
        f"{view.model_dump_json(indent=2)}"
    )
    if view.status in {TaskStatus.queued, TaskStatus.routing, TaskStatus.running}:
        text += "\n\nTask is still running. Call `status` with this task_id to get the result."
    return text


def build_server(
    settings: Settings, *, client_factory: ClientFactory | None = None, ensure=ensure_daemon
) -> MCPServer:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    server = MCPServer(
        "agent-dispatch",
        instructions=(
            "Route and dispatch coding tasks. Do not re-dispatch a task that is already "
            "delegated unless AgentDispatch explicitly allows escalation."
        ),
        log_level="WARNING",
    )
    factory = client_factory or (lambda state, timeout: DispatchClient(state, timeout))

    async def get_client(wait: int) -> DispatchClient:
        result = ensure(settings)
        state = await result if inspect.isawaitable(result) else result
        return factory(state, wait + 30)

    @server.tool(description="Choose the best executor for a task without running it.")
    async def route(
        task: str,
        cwd: str,
        context: str | None = None,
        files: list[str] | None = None,
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        context_mode: str = "prompt+summary",
    ) -> str:
        try:
            client = await get_client(settings.mcp.wait_seconds)
            decision = await client.route(
                _request(
                    task,
                    cwd,
                    settings,
                    context=context,
                    files=files or [],
                    constraints=constraints or [],
                    success_criteria=success_criteria or [],
                    context_mode=ContextMode(context_mode),
                )
            )
            return (
                f"executor: {decision.executor}\nconfidence: {decision.confidence}\n"
                f"router: {decision.router}\nreason: {decision.reason}\n\n"
                f"{decision.model_dump_json(indent=2)}"
            )
        except (DaemonUnavailable, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    async def do_dispatch(
        executor: str | None,
        task: str,
        cwd: str,
        context: str | None,
        files: list[str] | None,
        constraints: list[str] | None,
        success_criteria: list[str] | None,
        context_mode: str,
        allow_escalation: bool,
        wait_seconds: int | None,
        timeout_seconds: int | None,
    ) -> str:
        try:
            wait = settings.mcp.wait_seconds if wait_seconds is None else wait_seconds
            client = await get_client(wait)
            req = _request(
                task,
                cwd,
                settings,
                context=context,
                files=files or [],
                constraints=constraints or [],
                success_criteria=success_criteria or [],
                context_mode=ContextMode(context_mode),
                executor=executor,
                allow_escalation=allow_escalation,
                wait_seconds=wait,
                timeout_seconds=timeout_seconds,
            )
            return _task_text(await client.submit(req))
        except (DaemonUnavailable, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "Dispatch a coding task to the selected executor. Do not re-dispatch an "
            "already delegated task unless escalation is allowed."
        )
    )
    async def dispatch(
        task: str,
        cwd: str,
        context: str | None = None,
        files: list[str] | None = None,
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        context_mode: str = "prompt+summary",
        allow_escalation: bool = True,
        wait_seconds: int | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        return await do_dispatch(
            None,
            task,
            cwd,
            context,
            files,
            constraints,
            success_criteria,
            context_mode,
            allow_escalation,
            wait_seconds,
            timeout_seconds,
        )

    @server.tool(
        description=(
            "Dispatch a coding task to a specific executor. Use only when the executor "
            "is known; do not re-dispatch delegated work unless escalation is allowed."
        )
    )
    async def dispatch_to(
        executor: str,
        task: str,
        cwd: str,
        context: str | None = None,
        files: list[str] | None = None,
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        context_mode: str = "prompt+summary",
        allow_escalation: bool = True,
        wait_seconds: int | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        return await do_dispatch(
            executor,
            task,
            cwd,
            context,
            files,
            constraints,
            success_criteria,
            context_mode,
            allow_escalation,
            wait_seconds,
            timeout_seconds,
        )

    @server.tool(description="Get the current result of a delegated task by task_id.")
    async def status(task_id: str) -> str:
        try:
            client = await get_client(settings.mcp.wait_seconds)
            return _task_text(await client.status(task_id))
        except (DaemonUnavailable, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return server


def main() -> None:
    build_server(load_settings()).run("stdio")
