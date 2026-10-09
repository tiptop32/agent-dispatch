"""Follow-up: какое звено продолжать, с каким запросом и что сказать исполнителю.

Чистые функции над записями задач; походы в базу и запуск остаются в
`Dispatcher`.
"""

from __future__ import annotations

from agent_dispatch.dispatch.task_package import FOLLOWUP_SUMMARY_LIMIT, Followup
from agent_dispatch.models import FINAL_STATUSES, DispatchRequest, FollowupRequest, TaskRecord

#: Сколько хвоста вывода упавшей проверки уходит в промпт follow-up.
VERIFY_TAIL_LIMIT = 1500


class FollowupError(ValueError):
    """Продолжить задачу нельзя: она ещё идёт или её работы нет в рабочей копии."""


def continued_link(chain: list[TaskRecord]) -> TaskRecord:
    """Звено цепочки эскалации, которое продолжает follow-up.

    Это последнее звено, дошедшее до исполнителя: его работа и лежит в рабочей
    копии. Звено, которому guard отказал до запуска (бюджет, нет доступных),
    работы не несёт и пропускается.
    """
    last = chain[-1]
    if last.status not in FINAL_STATUSES:
        raise FollowupError(f"task {last.task_id} is still {last.status.value}")
    ran = [link for link in chain if _reached_executor(link)]
    if not ran:
        raise FollowupError(f"task {last.task_id} never reached an executor")
    link = ran[-1]
    meta = link.result.meta if link.result else {}
    # Работа вне рабочей копии: патч, ветка или оставшееся дерево (сбой
    # коммита, отмена, перезапуск демона пишут только путь и ветку).
    if meta.get("integrated") is False and any(
        meta.get(key) for key in ("commit", "patch", "worktree", "branch")
    ):
        where = meta.get("patch") or meta.get("branch") or meta.get("worktree")
        raise FollowupError(
            f"work of task {link.task_id} is not in the working copy (integrated: false): "
            f"apply {where} first or dispatch a new task"
        )
    return link


def _reached_executor(link: TaskRecord) -> bool:
    return link.decision is not None and link.result is not None and bool(link.result.executor)


def followup_request(
    link: TaskRecord, body: FollowupRequest, executor: str | None
) -> DispatchRequest:
    """Запрос follow-up: запрос звена с новым сообщением, всегда in_place.

    Worktree от HEAD не увидел бы перенесённого, но не закоммиченного
    результата. Заданный в `body` источник заменяет источник, hop и родителя
    звена: guards судят того, кто вызывает сейчас.
    """
    previous = link.request
    caller: dict[str, object] = {}
    if body.source_agent is not None:
        caller = {
            "source_agent": body.source_agent,
            "parent_task_id": body.parent_task_id,
            "root_agent": body.root_agent,
            "hop": body.hop,
        }
    return previous.model_copy(
        update={
            **caller,
            "task": body.message,
            "executor": executor,
            "followup_of": link.task_id,
            "allow_escalation": body.allow_escalation,
            "wait_seconds": body.wait_seconds,
            "timeout_seconds": body.timeout_seconds or previous.timeout_seconds,
            "verify": previous.verify if body.verify is None else body.verify,
            "workspace_mode": "in_place",
        }
    )


def describe(previous: TaskRecord, original: TaskRecord, *, resumed: bool) -> Followup:
    """Итог прошлой попытки для промпта.

    `original` — начало цепочки follow-up: `task` прошлого звена это уже
    сообщение, и без исходной задачи исполнитель без сессии не знал бы, о чём речь.
    """
    result = previous.result
    assert result is not None and previous.decision is not None
    verification = None
    if result.verification and result.verification.result == "failed":
        failed = result.verification.commands[-1] if result.verification.commands else None
        if failed is not None:
            verification = (
                f"`{failed.command}` failed ({failed.failure_reason}):\n"
                f"{failed.output_tail[-VERIFY_TAIL_LIMIT:]}"
            )
    summary = result.summary
    if len(summary) > FOLLOWUP_SUMMARY_LIMIT:
        summary = "... " + summary[-FOLLOWUP_SUMMARY_LIMIT:]
    return Followup(
        task_id=previous.task_id,
        executor=previous.decision.executor,
        status=previous.status.value,
        resumed=resumed,
        task=previous.request.task[:FOLLOWUP_SUMMARY_LIMIT],
        original_task=(
            original.request.task[:FOLLOWUP_SUMMARY_LIMIT]
            if original.task_id != previous.task_id
            else ""
        ),
        summary=summary,
        changed_files=result.changed_files,
        error=(result.error or "")[:500] or None,
        verification=verification,
    )
