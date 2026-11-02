"""Миграция пользовательского конфига с исполнителей codex/* и opencode/* на omp/*.

Команда `agent-dispatch migrate-config` переписывает записи и ссылки, сохраняет
бэкап рядом и работает идемпотентно: повторный запуск ничего не меняет.
Комментарии YAML не сохраняются (PyYAML их не переносит) — об этом команда
говорит в выводе, оригинал остаётся в бэкапе.
"""

from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from agent_dispatch.config import ConfigError, _config_path

#: Таблица переименований codex/*: старое имя -> (новое имя, модель agent-lb).
CODEX_EXECUTOR_MAP = {
    "codex/sol": ("omp/sol", "agent-lb/gpt-5.6-sol"),
    "codex/terra": ("omp/terra", "agent-lb/gpt-6.1-terra"),
    "codex/luna": ("omp/luna", "agent-lb/gpt-5.6-luna"),
    "codex/gpt-5.5": ("omp/gpt-5.5", "agent-lb/gpt-5.5"),
}
#: Голый `codex` (настройка дефолтного исполнителя) превращается в omp/sol.
BARE_CODEX_TARGET = "omp/sol"
BARE_CODEX_MODEL = "agent-lb/gpt-5.6-sol"
#: Префикс корпоративного провайдера omp для моделей из opencode-провайдера copilot.
X5_PROVIDER = "copilot-x5"
#: Флаг omp для thinking-вариантов моделей.
THINKING_ARGS = ["--thinking", "high"]

_FIELDS_DROPPED = {"adapter", "command", "model", "extra_args"}


@dataclass
class Migration:
    """Результат разбора пользовательского конфига.

    `config` — новый словарь конфига (исполнители — только живые записи);
    `items` — записи executors в исходном порядке: (имя, поля, комментарий).
    Комментарий не None значит, что запись выписывается в файл закомментированной.
    """

    config: dict
    items: list[tuple[str, dict, str | None]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    changed: bool = False


def _omp_executor_fields(old: dict, model: str, *, lb_group: bool) -> dict:
    """Поля omp-исполнителя из старой записи: сохраняемое переносим, CLI-специфику меняем."""
    fields = {key: value for key, value in old.items() if key not in _FIELDS_DROPPED}
    fields["adapter"] = "omp"
    fields["command"] = "omp"
    fields["model"] = model
    if lb_group:
        fields["limit_group"] = "agent-lb"
        fields["limit_reset"] = "recheck"
    return fields


def _migrate_codex_executor(name: str, old: dict, migration: Migration) -> tuple[str, dict] | None:
    """codex/* -> omp/*; без модели вне встроенной таблицы запись не восстанавливается."""
    target = CODEX_EXECUTOR_MAP.get(name)
    if target is not None:
        new_name, model = target
        return new_name, _omp_executor_fields(old, model, lb_group=True)
    model = old.get("model")
    if not isinstance(model, str) or not model:
        migration.notes.append(
            f"{name}: неизвестный исполнитель codex без модели не перенесён, запись выключена"
        )
        return None
    migration.notes.append(f"{name}: модель {model} перенесена на agent-lb/{model}")
    return (
        f"omp/{name.split('/', 1)[1]}",
        _omp_executor_fields(old, f"agent-lb/{model}", lb_group=True),
    )


def _migrate_opencode_executor(
    name: str, old: dict, migration: Migration
) -> tuple[str, dict] | None:
    """opencode/* -> omp/*: провайдер copilot -> copilot-x5, остальные выключаются."""
    model = old.get("model")
    if "/" not in name or not isinstance(model, str) or not model:
        migration.notes.append(
            f"{name}: у записи opencode нет модели, исполнитель выключен при миграции"
        )
        return None
    provider, _, model_id = model.partition("/")
    if provider != "copilot":
        migration.notes.append(
            f"{name}: провайдер {provider} из opencode не имеет аналога в omp, "
            "исполнитель выключен при миграции"
        )
        return None
    extra_args = old.get("extra_args")
    extra = list(extra_args) if isinstance(extra_args, list) else []
    if model_id.endswith("-thinking"):
        # omp выбирает уровень рассуждений флагом, а не отдельной моделью.
        model_id = model_id[: -len("-thinking")]
        extra = list(THINKING_ARGS)
    fields = _omp_executor_fields(old, f"{X5_PROVIDER}/{model_id}", lb_group=False)
    fields["extra_args"] = extra
    return f"omp/{name.split('/', 1)[1]}", fields


def _migrate_executors(config: dict, migration: Migration) -> None:
    executors = config.get("executors")
    if not isinstance(executors, dict):
        return
    # Голый `codex` в ссылках (fallback, escalation) всегда означает omp/sol.
    name_map: dict[str, str] = {"codex": BARE_CODEX_TARGET}
    removed: set[str] = set()
    has_slashed_codex = any(
        isinstance(fields, dict) and fields.get("adapter") == "codex" and "/" in name
        for name, fields in executors.items()
    )
    items: list[tuple[str, dict, str | None]] = []
    for name, old in executors.items():
        old = old if isinstance(old, dict) else {}
        adapter = old.get("adapter")
        if adapter == "codex" or name == "codex":
            if name == "codex":
                if has_slashed_codex:
                    # Голый `codex` выключал дефолтного исполнителя, которого после
                    # миграции нет; конкретные codex/* уже переехали в omp/*.
                    migration.notes.append(
                        "codex: запись дефолтного исполнителя пропущена, "
                        f"{BARE_CODEX_TARGET} определён записями codex/*"
                    )
                    removed.add(name)
                    items.append(
                        (name, old, "дефолтный исполнитель codex после миграции не существует")
                    )
                    continue
                items.append(
                    (
                        BARE_CODEX_TARGET,
                        _omp_executor_fields(old, BARE_CODEX_MODEL, lb_group=True),
                        None,
                    )
                )
                continue
            mapped = _migrate_codex_executor(name, old, migration)
            if mapped is None:
                removed.add(name)
                items.append(
                    (name, old, "исполнитель codex без модели не переносится в omp, выключен")
                )
                continue
            new_name, fields = mapped
            name_map[name] = new_name
            items.append((new_name, fields, None))
            continue
        if adapter == "opencode" or name == "opencode" or name.startswith("opencode/"):
            mapped = _migrate_opencode_executor(name, old, migration)
            if mapped is None:
                removed.add(name)
                items.append(
                    (
                        name,
                        old,
                        f"{name}: провайдер opencode не имеет аналога в omp, запись выключена",
                    )
                )
                continue
            new_name, fields = mapped
            name_map[name] = new_name
            items.append((new_name, fields, None))
            continue
        items.append((name, old, None))
    migration.items = items
    _migrate_references(config, migration, name_map, removed)


def _mapped(value: str, name_map: dict[str, str]) -> str:
    return name_map.get(value, value)


def _migrate_references(
    config: dict, migration: Migration, name_map: dict[str, str], removed: set[str]
) -> None:
    """Переписать имена исполнителей в fallback_executor, escalation и review_only."""
    routing = config.get("routing")
    if isinstance(routing, dict) and "fallback_executor" in routing:
        value = routing["fallback_executor"]
        if isinstance(value, str):
            routing["fallback_executor"] = _mapped(value, name_map)
        elif isinstance(value, list):
            kept = []
            for item in value:
                if isinstance(item, str) and item in removed:
                    migration.notes.append(
                        f"routing.fallback_executor: {item} выключен и убран из цепочки"
                    )
                    continue
                kept.append(_mapped(item, name_map) if isinstance(item, str) else item)
            routing["fallback_executor"] = kept
    escalation = config.get("escalation")
    if isinstance(escalation, dict):
        migrated: dict[str, list[str]] = {}
        for source, targets in escalation.items():
            if isinstance(source, str) and source in removed:
                migration.notes.append(f"escalation.{source}: исполнитель выключен, цепочка убрана")
                continue
            kept = []
            for target in targets:
                if isinstance(target, str) and target in removed:
                    migration.notes.append(
                        f"escalation.{source}: выключенный {target} убран из цепочки"
                    )
                    continue
                kept.append(_mapped(target, name_map) if isinstance(target, str) else target)
            if kept:
                migrated[_mapped(source, name_map) if isinstance(source, str) else source] = kept
        config["escalation"] = migrated
    if isinstance(routing, dict):
        review_only = routing.get("review_only")
        if isinstance(review_only, dict) and "codex" in review_only:
            # codex как источник теперь зовётся omp; старое имя остаётся валидным
            # для истории, но ссылаться на него в новом конфиге незачем.
            targets = review_only.pop("codex")
            existing = review_only.get("omp")
            review_only["omp"] = list(dict.fromkeys([*(existing or []), *targets]))


def migrate_dict(user: dict) -> Migration:
    """Переписывает разобранный пользовательский конфиг; исходный словарь не меняет."""
    migration = Migration(config=copy.deepcopy(user))
    _migrate_executors(migration.config, migration)
    migration.config["executors"] = {
        name: fields for name, fields, comment in migration.items if comment is None
    }
    migration.changed = migration.config != user
    return migration


def _render_executors(items: list[tuple[str, dict, str | None]]) -> str:
    lines = ["executors:"]
    for name, fields, comment in items:
        block = yaml.safe_dump(
            {name: fields}, sort_keys=False, allow_unicode=True, default_flow_style=False
        ).rstrip("\n")
        prefix = "  # " if comment is not None else "  "
        if comment is not None:
            lines.append(f"  # {comment}")
        lines.extend(prefix + line for line in block.splitlines())
    return "\n".join(lines)


def render(migration: Migration) -> str:
    """Текст мигрированного конфига; закомментированные записи остаются на своих местах."""
    parts: list[str] = []
    for key, value in migration.config.items():
        if key == "executors":
            parts.append(_render_executors(migration.items))
        else:
            block = yaml.safe_dump(
                {key: value}, sort_keys=False, allow_unicode=True, default_flow_style=False
            ).rstrip("\n")
            parts.append(block)
    return "\n\n".join(parts) + "\n"


def migrate_file(config_dir: Path | None = None, dry_run: bool = False) -> tuple[int, str]:
    """Мигрирует config.yaml; возвращает (код выхода, текст отчёта).

    Без изменений файл не перезаписывается и бэкап не создаётся. `dry_run`
    печатает unified diff и ничего не пишет.
    """
    directory = _config_path(config_dir)
    path = directory / "config.yaml"
    if not path.is_file():
        return 1, f"{path}: пользовательский конфиг не найден, мигрировать нечего\n"
    old_text = path.read_text()
    try:
        user = yaml.safe_load(old_text)
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: invalid YAML: {error}") from error
    if user is None:
        user = {}
    if not isinstance(user, dict):
        return 1, f"{path}: верхний уровень конфига должен быть отображением\n"
    migration = migrate_dict(user)
    if not migration.changed:
        return 0, f"{path}: конфиг уже мигрирован на omp, изменений нет\n"
    new_text = render(migration)
    report_lines = [f"Заметки миграции {path}:"]
    report_lines.extend(f"- {note}" for note in migration.notes)
    if "#" in old_text:
        report_lines.append(
            "- комментарии YAML не сохранены (PyYAML их не переносит), "
            "оригинал лежит в бэкапе рядом"
        )
    if dry_run:
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{path} (сейчас)",
            tofile=f"{path} (после миграции)",
        )
        report_lines.append("dry-run: файлы не изменены, дифф:")
        report_lines.append("".join(diff))
        return 0, "\n".join(report_lines) + "\n"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    backup.write_text(old_text)
    path.write_text(new_text)
    report_lines.append(f"бэкап: {backup}")
    report_lines.append(f"записано: {path}")
    return 0, "\n".join(report_lines) + "\n"
