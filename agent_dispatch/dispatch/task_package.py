from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict

from agent_dispatch.config import Settings
from agent_dispatch.models import ContextMode, DispatchRequest

AdapterKind = Literal["claude", "codex", "opencode"]


class TaskPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: DispatchRequest
    git_status: str | None = None
    git_diff_stat: str | None = None


def _git(cwd: str, args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout


def build_task_package(req: DispatchRequest, settings: Settings) -> TaskPackage:
    if req.context_mode == ContextMode.full:
        return TaskPackage(
            request=req,
            git_status=_git(req.cwd, ["status", "--short"]),
            git_diff_stat=_git(req.cwd, ["diff", "--stat"]),
        )
    return TaskPackage(request=req)


def _bullets(value: list[str]) -> str:
    return "\n".join(f"- {item}" for item in value) if value else "- (none)"


def render_prompt(package: TaskPackage, adapter_kind: AdapterKind, settings: Settings) -> str:
    template_dir = Path(__file__).resolve().parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["bullets"] = _bullets
    req = package.request
    if adapter_kind == "codex":
        instructions = "Report the outcome using the structured output schema: status (completed|partial|failed|needs_context|needs_escalation), summary, changed_files, tests {command, result}, confidence (0..1), needs_escalation."  # noqa: E501
    else:
        instructions = 'End your final message with a fenced block tagged `agent-dispatch-result` containing a JSON object with fields: status (completed|partial|failed|needs_context|needs_escalation), summary, changed_files, tests {command, result}, confidence (0..1), needs_escalation. Example:\n```agent-dispatch-result\n{"status": "completed", "summary": "...", "changed_files": [], "tests": {"command": "pytest", "result": "passed"}, "confidence": 0.9, "needs_escalation": false}\n```'  # noqa: E501
    return env.get_template("task_package.md.j2").render(
        task=req.task,
        cwd=req.cwd,
        context=req.context,
        files=req.files,
        constraints=req.constraints,
        success_criteria=req.success_criteria,
        context_mode=req.context_mode.value,
        git_status=package.git_status or "",
        git_diff_stat=package.git_diff_stat or "",
        source_agent=req.source_agent.value,
        hop=req.hop,
        max_hops=settings.routing.max_hops,
        max_children=settings.routing.max_children,
        result_instructions=instructions,
    )
