from __future__ import annotations

import inspect
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agent_dispatch.config import Settings, load_settings
from agent_dispatch.mcp.autostart import ensure_daemon
from agent_dispatch.mcp.client import DaemonUnavailable, DispatchClient
from agent_dispatch.models import (
    ContextMode,
    DispatchRequest,
    FollowupRequest,
    RouteDecision,
    RouterKind,
    SourceAgent,
    TaskStatus,
    TaskView,
    VerificationInfo,
)
from agent_dispatch.serve_state import ServeState

ClientFactory = Callable[[ServeState, float], DispatchClient]


def _agent(value: str | None, default: SourceAgent | None = None) -> SourceAgent | None:
    try:
        return SourceAgent(value) if value else default
    except ValueError:
        return default


KIND_HINT = (
    "kind='task' (default) changes code; kind='review' asks for a read-only review "
    "of the working copy and returns findings without editing files."
)
VERIFY_HINT = (
    " Pass `verify` with shell commands (e.g. the test command for the touched scope, "
    "written for `cwd`): AgentDispatch runs them itself after the executor, and a "
    "failure counts as not done and escalates."
)


def _source() -> SourceAgent:
    return (
        _agent(os.environ.get("AGENT_DISPATCH_SOURCE_AGENT"), SourceAgent.unknown)
        or SourceAgent.unknown
    )


def _local_time(value: str | datetime) -> str:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone().isoformat(timespec="minutes")


def _executors_text(rows: list[dict], settings: Settings, source: SourceAgent) -> str:
    """Компактная сводка: кому можно отдать работу сейчас и почему нельзя остальным."""
    review_only = set(settings.routing.review_only.get(source.value, []))
    ready: list[str] = []
    cooling: dict[tuple[str, str, str], list[str]] = {}
    down: list[str] = []
    reviewers: list[str] = []
    for row in rows:
        if not row.get("enabled"):
            continue
        name = row["name"]
        cooldown = row.get("cooldown")
        if cooldown:
            key = (_local_time(cooldown["until"]), cooldown["kind"], cooldown["detail"][:120])
            cooling.setdefault(key, []).append(name)
        elif row.get("available") is False:
            down.append(f"{name} ({(row.get('error') or 'unavailable')[:80]})")
        else:
            ready.append(name)
        if row.get("adapter") in review_only:
            reviewers.append(name)
    lines = [f"available: {', '.join(ready) or '(none)'}"]
    for (until, kind, detail), names in cooling.items():
        lines.append(f"out until {until} ({kind}): {', '.join(names)}; {detail}")
    if down:
        lines.append(f"unavailable: {', '.join(down)}")
    if reviewers:
        lines.append(f"review only from {source.value} (kind='review'): {', '.join(reviewers)}")
    return "\n".join(lines)


def _request(task: str, cwd: str, settings: Settings, **kwargs: Any) -> DispatchRequest:
    source = _source()
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


def _route_lines(decision: RouteDecision) -> list[str]:
    route_parts = [str(decision.router)]
    if decision.capability:
        route_parts.append(decision.capability)
    confidence = str(decision.confidence)
    if decision.confidence_tier:
        confidence += f" {decision.confidence_tier}"
    route_parts.append(confidence)
    route = f"route: {' · '.join(route_parts)}"
    if decision.router == RouterKind.fallback and decision.reason:
        route += f" (reason: {decision.reason})"

    lines = [route]
    notes = decision.meta.get("selection_notes")
    if notes:
        lines.append(f"notes: {'; '.join(notes)}")
    return lines


#: Поля `result.meta`, которые компактный ответ показывает вызывающему.
_META_LINES = (
    "warning",
    "resume_failed",
    "cooldown_until",
    "budget",
    "branch",
    "worktree",
    "patch",
    "integration_error",
)


def _verification_line(info: VerificationInfo) -> str:
    """Итог проверки демона одной строкой; вывод упавшей команды обрезан."""
    text = f"verified: {info.result}"
    if info.result == "failed" and info.commands:
        failed = info.commands[-1]
        how = "timeout" if failed.timed_out else f"exit {failed.exit_code}"
        tail = " | ".join(line for line in failed.output_tail.splitlines()[-3:] if line.strip())
        text += f" ({failed.command}: {how}){f' {tail[-300:]}' if tail else ''}"
    if info.contradicts_report:
        text += "; executor reported tests passed"
    if info.tests_changed:
        text += f"; tests changed: {', '.join(info.tests_changed)}"
    return text


def _task_text(view: TaskView, verbose: bool = False) -> str:
    result = view.result
    executor = (
        result.executor
        if result and result.executor
        else (view.decision.executor if view.decision else "")
    )
    changed = ", ".join(result.changed_files) if result else ""
    summary = result.summary if result else ""
    if verbose:
        text = (
            f"task_id: {view.task_id}\nstatus: {view.status.value}\n"
            f"executor: {executor}\nchanged_files: {changed}\nsummary: {summary}\n\n"
            f"{view.model_dump_json(indent=2)}"
        )
        if view.status in {TaskStatus.queued, TaskStatus.routing, TaskStatus.running}:
            text += "\n\nTask is still running. Call `status` with this task_id to get the result."
        return text

    lines = [f"task_id: {view.task_id}", f"status: {view.status.value}"]
    if executor:
        lines.append(f"executor: {executor}")
    if view.decision:
        lines.extend(_route_lines(view.decision))
    if view.escalated_from:
        lines.append(f"escalated_from: {view.escalated_from}")
    if view.request.followup_of:
        lines.append(f"followup_of: {view.request.followup_of}")
    if result:
        escalated_to = result.meta.get("escalated_to")
        if escalated_to:
            lines.append(f"escalated_to: {escalated_to}")
        if changed:
            lines.append(f"changed_files: {changed}")
        if result.tests and result.tests.result:
            tests = result.tests.result
            if result.tests.command:
                tests += f" ({result.tests.command})"
            lines.append(f"tests: {tests}")
        if result.verification:
            lines.append(_verification_line(result.verification))
        if result.error:
            lines.append(f"error: {result.error[:500]}")
        for key in _META_LINES:
            if value := result.meta.get(key):
                lines.append(f"{key}: {value}")
        if summary:
            if len(summary) > 800:
                summary = summary[:800] + " …"
            lines.append(f"summary: {summary}")

    show_log = view.status in {
        TaskStatus.queued,
        TaskStatus.routing,
        TaskStatus.running,
        TaskStatus.failed,
        TaskStatus.partial,
    }
    log_lines = [line[:200] for line in view.log_tail.splitlines() if line.strip()][-5:]
    if show_log and log_lines:
        lines.append("log_tail:\n" + "\n".join(log_lines))
    if view.status in {TaskStatus.queued, TaskStatus.routing, TaskStatus.running}:
        lines.append(
            "Task is still running. Call `status` with this task_id and wait_seconds "
            "(up to 600) to block until it finishes."
        )
    return "\n".join(lines)


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

    @server.tool(description="Choose the best executor for a task without running it. " + KIND_HINT)
    async def route(
        task: str,
        cwd: str,
        context: str | None = None,
        files: list[str] | None = None,
        constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        context_mode: str = "prompt+summary",
        kind: str = "task",
        verbose: bool = False,
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
                    kind=kind,
                )
            )
            if verbose:
                return (
                    f"executor: {decision.executor}\nconfidence: {decision.confidence}\n"
                    f"router: {decision.router}\nreason: {decision.reason}\n\n"
                    f"{decision.model_dump_json(indent=2)}"
                )
            return "\n".join([f"executor: {decision.executor}", *_route_lines(decision)])
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
        verbose: bool,
        kind: str = "task",
        verify: list[str] | None = None,
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
                kind=kind,
                verify=verify or [],
            )
            return _task_text(await client.submit(req), verbose)
        except (DaemonUnavailable, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "Dispatch a coding task to the selected executor. Do not re-dispatch an "
            "already delegated task unless escalation is allowed. " + KIND_HINT + VERIFY_HINT
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
        kind: str = "task",
        verify: list[str] | None = None,
        verbose: bool = False,
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
            verbose,
            kind,
            verify,
        )

    @server.tool(
        description=(
            "Dispatch a coding task to a specific executor. Use only when the executor "
            "is known; do not re-dispatch delegated work unless escalation is allowed. "
            "Executors out of usage limits are refused: call `executors` first. "
            + KIND_HINT
            + VERIFY_HINT
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
        kind: str = "task",
        verify: list[str] | None = None,
        verbose: bool = False,
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
            verbose,
            kind,
            verify,
        )

    @server.tool(
        description=(
            "List executors that can take work right now. Shows which are out of usage "
            "limits (and until when) and which take only reviews from you."
        )
    )
    async def executors() -> str:
        try:
            client = await get_client(settings.mcp.wait_seconds)
            rows = await client.executors()
        except (DaemonUnavailable, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return _executors_text(rows, settings, _source())

    @server.tool(
        description=(
            "Continue a finished delegated task with a new message instead of dispatching it "
            "again: the same executor picks it up in its own CLI session when possible, "
            "otherwise with the previous attempt's report. Use it for corrections, a failed "
            "check or a timeout. `verify` defaults to the previous task's commands."
        )
    )
    async def followup(
        task_id: str,
        message: str,
        verify: list[str] | None = None,
        allow_escalation: bool = True,
        wait_seconds: int | None = None,
        timeout_seconds: int | None = None,
        verbose: bool = False,
    ) -> str:
        try:
            wait = settings.mcp.wait_seconds if wait_seconds is None else wait_seconds
            client = await get_client(wait)
            body = FollowupRequest(
                message=message,
                verify=verify,
                allow_escalation=allow_escalation,
                wait_seconds=wait,
                timeout_seconds=timeout_seconds,
            )
            return _task_text(await client.followup(task_id, body), verbose)
        except (DaemonUnavailable, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="Get the current result of a delegated task by task_id.")
    async def status(task_id: str, wait_seconds: int = 0, verbose: bool = False) -> str:
        try:
            wait = max(0, wait_seconds)
            client = await get_client(wait)
            return _task_text(await client.status(task_id, wait), verbose)
        except (DaemonUnavailable, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return server


def main() -> None:
    build_server(load_settings()).run("stdio")
