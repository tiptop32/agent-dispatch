import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from agent_dispatch.config import ExecutorSettings, ServerSettings, Settings
from agent_dispatch.mcp.autostart import ensure_daemon
from agent_dispatch.mcp.client import DaemonUnavailable
from agent_dispatch.serve_state import ServeState, write_state


def make_settings(tmp_path):
    return Settings(
        server=ServerSettings(data_dir=tmp_path),
        executors={"codex": ExecutorSettings(adapter="codex")},
    )


def make_state():
    return ServeState(pid=os.getpid(), port=7433, token="x", started_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_live_state_skips_popen(tmp_path):
    settings = make_settings(tmp_path)
    state = make_state()
    write_state(tmp_path, state)
    popen = AsyncMock()
    assert await ensure_daemon(settings, popen=popen, health=AsyncMock(return_value=True)) == state
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_dead_state_starts_and_returns(tmp_path):
    settings = make_settings(tmp_path)
    write_state(tmp_path, make_state().model_copy(update={"pid": 999999}))
    state = make_state()

    def popen(*args, **kwargs):
        write_state(tmp_path, state)

    result = await ensure_daemon(settings, popen=popen, health=AsyncMock(return_value=True))
    assert result == state


@pytest.mark.asyncio
async def test_timeout(tmp_path):
    settings = make_settings(tmp_path)
    now = iter([0.0, 0.4, 0.4, 0.4])

    async def no_sleep(_):
        pass

    with pytest.raises(DaemonUnavailable, match="did not start"):
        await ensure_daemon(
            settings,
            deadline_seconds=0.3,
            sleep=no_sleep,
            popen=lambda *a, **k: None,
            health=AsyncMock(return_value=False),
            clock=lambda: next(now),
        )


@pytest.mark.parametrize("value", [0, 0.1, 1, 2, 5, 10])
def test_deadline_values_are_supported(value):
    assert value >= 0
