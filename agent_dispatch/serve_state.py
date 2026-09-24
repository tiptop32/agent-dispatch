"""Состояние запущенного демона: serve.json с pid, портом и токеном (права 0600)."""

from __future__ import annotations

import os
import socket
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class ServeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int
    port: int
    host: str = "127.0.0.1"
    token: str
    started_at: datetime


def state_path(data_dir: Path) -> Path:
    return data_dir / "serve.json"


def write_state(data_dir: Path, state: ServeState) -> Path:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Файл содержит токен, поэтому создаём его сразу с правами 0600.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(state.model_dump_json(indent=2))
    os.chmod(path, 0o600)
    return path


def read_state(data_dir: Path) -> ServeState | None:
    path = state_path(data_dir)
    if not path.is_file():
        return None
    try:
        return ServeState.model_validate_json(path.read_text())
    except (ValidationError, ValueError, OSError):
        return None


def clear_state(data_dir: Path) -> None:
    try:
        state_path(data_dir).unlink()
    except FileNotFoundError:
        pass


def is_alive(state: ServeState) -> bool:
    """Жив ли процесс с pid из состояния (сигнал 0)."""
    try:
        os.kill(state.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_in_use(host: str, port: int) -> bool:
    """Занят ли порт: пробный bind, тот же, что делает uvicorn при старте."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError:
        return True
    return False


def is_running(state: ServeState) -> bool:
    """Обслуживает ли демон из состояния запросы.

    Одного pid мало: `os.kill(pid, 0)` истинен и для зомби, которого никто не
    похоронил, и для процесса, застрявшего в shutdown. Такой pid запирал старт
    нового демона навсегда: `serve` видел «уже запущен», а запросы при этом
    некому было обслуживать. Порт отвечает честно — сокетов ни зомби, ни
    завершившийся uvicorn не держат.
    """
    return is_alive(state) and port_in_use(state.host, state.port)
