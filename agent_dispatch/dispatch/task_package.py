from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict

from agent_dispatch.config import Settings
from agent_dispatch.executors.workspace import git_env
from agent_dispatch.models import ContextMode, DispatchRequest

AdapterKind = Literal["claude", "codex", "opencode"]


class Followup(BaseModel):
    """Продолжаемая задача: что исполнитель уже сделал и чем это кончилось."""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    executor: str
    status: str
    #: Исполнитель продолжает свою сессию CLI: Task Package у него уже есть,
    #: поэтому промпт короткий. Иначе итог прошлой попытки идёт в промпт.
    resumed: bool = False
    task: str = ""
    summary: str = ""
    changed_files: list[str] = []
    error: str | None = None
    verification: str | None = None


#: Сколько символов прошлого отчёта уходит в промпт follow-up без сессии.
FOLLOWUP_SUMMARY_LIMIT = 3000


class TaskPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: DispatchRequest
    git_status: str | None = None
    git_diff_stat: str | None = None
    #: Дифф рабочей копии против HEAD для ревью: ревьюер работает без Bash.
    git_diff: str | None = None
    #: Рабочий каталог исполнителя, если это не сам cwd запроса.
    worktree: str | None = None
    #: Корень репозитория вызывающего: исполнитель стартует в корне worktree,
    #: даже если cwd запроса это подкаталог, поэтому пути отображаются от корня.
    repo_root: str | None = None
    branch: str | None = None
    followup: Followup | None = None


def _git(cwd: str, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True, check=False
    )
    return result.stdout


def build_task_package(
    req: DispatchRequest,
    settings: Settings,
    worktree: str | None = None,
    branch: str | None = None,
) -> TaskPackage:
    if req.kind == "review":
        diff = _git(req.cwd, ["diff", "HEAD"])
        if len(diff) > REVIEW_DIFF_LIMIT:
            diff = diff[:REVIEW_DIFF_LIMIT] + "\n... (diff truncated, read the files directly)\n"
        return TaskPackage(
            request=req,
            git_status=_git(req.cwd, ["status", "--short"]),
            git_diff=diff,
            worktree=worktree,
            repo_root=_repo_root(req, worktree),
            branch=branch,
        )
    if req.context_mode == ContextMode.full:
        return TaskPackage(
            request=req,
            git_status=_git(req.cwd, ["status", "--short"]),
            git_diff_stat=_git(req.cwd, ["diff", "--stat"]),
            worktree=worktree,
            repo_root=_repo_root(req, worktree),
            branch=branch,
        )
    return TaskPackage(
        request=req, worktree=worktree, repo_root=_repo_root(req, worktree), branch=branch
    )


def _repo_root(req: DispatchRequest, worktree: str | None) -> str | None:
    if not worktree:
        return None
    return _git(req.cwd, ["rev-parse", "--show-toplevel"]).strip() or req.cwd


#: Сколько символов диффа уходит ревьюеру; дальше он читает файлы сам.
REVIEW_DIFF_LIMIT = 100_000


def _bullets(value: list[str]) -> str:
    return "\n".join(f"- {item}" for item in value) if value else "- (none)"


def rewrite_repo_prefix(text: str | None, repo_root: str, worktree: str) -> str | None:
    """Абсолютные пути исходной рабочей копии → пути worktree, только внутри префикса.

    Вызывающий пишет пути своего cwd (`/x/repo/...`) где угодно в тексте, а
    исполняется задача в worktree: без переписывания агент правит чужую рабочую
    копию мимо своего дерева. Граница пути строгая: за префиксом идёт `/`,
    конец текста, пробел или знак препинания, поэтому `/x/repo2`, `/x/repo-old`
    и `/x/repo.bak` не переписываются.
    """
    if not text:
        return text
    prefix = repo_root.rstrip("/")
    return re.sub(re.escape(prefix) + r"(?=$|/|[\s'\"`),;:\]}])", lambda _: worktree, text)


def _rewrite_file_list(files: list[str] | None, repo_root: str, worktree: str) -> list[str] | None:
    if not files:
        return files
    return [rewrite_repo_prefix(item, repo_root, worktree) or item for item in files]


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
    worktree = package.worktree
    task, context, files, checks = req.task, req.context, req.files, req.verify
    if worktree:
        # В worktree-режиме вызывающий мог написать абсолютные пути исходной
        # рабочей копии: переписываем их на пути worktree, иначе агент правит
        # чужую рабочую копию мимо своего дерева (opencode --auto одобряет
        # external_directory). Оба пути остаются в промпте.
        root = package.repo_root or req.cwd
        task = rewrite_repo_prefix(task, root, worktree)
        context = rewrite_repo_prefix(context, root, worktree)
        files = _rewrite_file_list(files, root, worktree)
        checks = _rewrite_file_list(checks, root, worktree)
    if adapter_kind == "codex":
        instructions = "Report the outcome using the structured output schema: status (completed|partial|failed|needs_context|needs_escalation), summary, changed_files, tests {command, result: passed|failed|not_run}, confidence (0..1), needs_escalation."  # noqa: E501
    else:
        instructions = 'End your final message with a fenced block tagged `agent-dispatch-result` containing a JSON object with fields: status (completed|partial|failed|needs_context|needs_escalation), summary, changed_files, tests {command, result: passed|failed|not_run}, confidence (0..1), needs_escalation. Example:\n```agent-dispatch-result\n{"status": "completed", "summary": "...", "changed_files": [], "tests": {"command": "pytest", "result": "passed"}, "confidence": 0.9, "needs_escalation": false}\n```'  # noqa: E501
    budget_seconds = req.timeout_seconds or settings.routing.default_timeout_seconds
    followup = package.followup
    template = "followup_resumed.md.j2" if followup and followup.resumed else "task_package.md.j2"
    return env.get_template(template).render(
        followup=followup,
        task=task,
        review=req.kind == "review",
        cwd=worktree or req.cwd,
        repo=req.cwd,
        worktree=bool(worktree),
        branch=package.branch,
        context=context,
        files=files,
        constraints=req.constraints,
        success_criteria=req.success_criteria,
        verify=checks,
        context_mode=req.context_mode.value,
        git_status=package.git_status or "",
        git_diff_stat=package.git_diff_stat or "",
        git_diff=package.git_diff or "",
        source_agent=req.source_agent.value,
        hop=req.hop,
        max_hops=settings.routing.max_hops,
        max_children=settings.routing.max_children,
        time_budget_seconds=budget_seconds,
        time_budget_minutes=max(1, round(budget_seconds / 60)),
        result_instructions=instructions,
    )
