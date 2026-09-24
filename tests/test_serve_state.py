import os
import socket
import stat
from datetime import UTC, datetime

from agent_dispatch.serve_state import (
    ServeState,
    clear_state,
    is_alive,
    is_running,
    read_state,
    state_path,
    write_state,
)


def _state(pid: int = 1) -> ServeState:
    return ServeState(pid=pid, port=7433, token="t0k", started_at=datetime.now(UTC))


def test_write_creates_file_with_0600(tmp_path):
    path = write_state(tmp_path / "data", _state())
    assert path == state_path(tmp_path / "data")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_round_trip(tmp_path):
    state = _state(pid=4242)
    write_state(tmp_path, state)
    assert read_state(tmp_path) == state


def test_read_missing_returns_none(tmp_path):
    assert read_state(tmp_path) is None


def test_read_invalid_json_returns_none(tmp_path):
    state_path(tmp_path).write_text("{not json")
    assert read_state(tmp_path) is None


def test_write_overwrites_and_keeps_mode(tmp_path):
    write_state(tmp_path, _state(pid=1))
    write_state(tmp_path, _state(pid=2))
    assert read_state(tmp_path).pid == 2
    assert stat.S_IMODE(state_path(tmp_path).stat().st_mode) == 0o600


def test_is_alive_current_process():
    assert is_alive(_state(pid=os.getpid())) is True


def test_is_alive_dead_pid():
    assert is_alive(_state(pid=2**22 - 1)) is False


def _listening() -> socket.socket:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    return holder


def test_is_running_needs_the_port_too():
    """Зомби и застрявший в shutdown процесс живы по pid, но порт уже отдали."""
    with _listening() as holder:
        free_port = holder.getsockname()[1]
    stale = ServeState(pid=os.getpid(), port=free_port, token="t", started_at=datetime.now(UTC))
    assert is_alive(stale) is True
    assert is_running(stale) is False


def test_is_running_when_the_port_answers():
    with _listening() as holder:
        state = ServeState(
            pid=os.getpid(),
            port=holder.getsockname()[1],
            token="t",
            started_at=datetime.now(UTC),
        )
        assert is_running(state) is True


def test_is_running_false_for_a_dead_pid():
    with _listening() as holder:
        state = ServeState(
            pid=2**22 - 1,
            port=holder.getsockname()[1],
            token="t",
            started_at=datetime.now(UTC),
        )
        assert is_running(state) is False


def test_clear_removes_and_tolerates_missing(tmp_path):
    write_state(tmp_path, _state())
    clear_state(tmp_path)
    assert not state_path(tmp_path).exists()
    clear_state(tmp_path)
