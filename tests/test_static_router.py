from pathlib import Path

import httpx
import pytest
import respx

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest, RouterKind
from agent_dispatch.routing.base import RouterError
from agent_dispatch.routing.decision import build_routers, decide_with_fallback
from agent_dispatch.routing.jev import JevRouter


@pytest.mark.asyncio
async def test_static_backend_selects_without_calling_external_router(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "router": {"backend": "static"},
            "routing": {"fallback_executor": "balanced"},
            "executors": {
                "balanced": {"adapter": "omp", "model": "m", "tier": "balanced"},
                "strong": {"adapter": "claude", "tier": "strong"},
            },
        }
    )
    client = httpx.AsyncClient()
    try:
        routers = build_routers(settings, client)
        decision, events = await decide_with_fallback(
            DispatchRequest(task="private task", cwd=str(tmp_path)),
            settings.enabled_executors(),
            settings,
            routers,
        )
    finally:
        await client.aclose()
    assert [router.name for router in routers] == ["static"]
    assert decision.router == RouterKind.static
    assert decision.executor == "balanced"
    assert not events


@pytest.mark.asyncio
@respx.mock
async def test_external_router_denies_untrusted_realpath_before_network(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    trusted.mkdir()
    outside.mkdir()
    link = trusted / "linked"
    link.symlink_to(outside, target_is_directory=True)
    route = respx.post("https://router.invalid/decide").respond(200, json={})
    settings = Settings.model_validate(
        {
            "router": {
                "backend": "jev",
                "external": {"trusted_roots": [str(trusted)]},
                "jev": {"base_url": "https://router.invalid/decide"},
            },
            "routing": {"fallback_executor": "balanced"},
            "executors": {
                "balanced": {"adapter": "omp", "model": "m", "tier": "balanced"},
            },
            "secrets": {"OPENROUTER_API_KEY": "test-key"},  # pragma: allowlist secret
        }
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="external router denied"):
            await JevRouter(settings, client).decide(
                DispatchRequest(task="must not leave machine", cwd=str(link)),
                settings.enabled_executors(),
            )
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_external_router_denies_unknown_project_by_default(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    route = respx.post("https://router.invalid/decide").respond(200, json={})
    settings = Settings.model_validate(
        {
            "router": {
                "backend": "jev",
                "jev": {"base_url": "https://router.invalid/decide"},
            },
            "routing": {"fallback_executor": "balanced"},
            "executors": {
                "balanced": {"adapter": "omp", "model": "m", "tier": "balanced"},
            },
            "secrets": {"OPENROUTER_API_KEY": "test-key"},  # pragma: allowlist secret
        }
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouterError, match="external router denied"):
            await JevRouter(settings, client).decide(
                DispatchRequest(task="unknown project", cwd=str(project)),
                settings.enabled_executors(),
            )
    assert not route.called


@pytest.mark.asyncio
async def test_static_backend_honors_explicit_capability(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "router": {"backend": "static"},
            "routing": {"fallback_executor": "fast"},
            "executors": {
                "fast": {"adapter": "omp", "model": "m", "tier": "fast"},
                "balanced": {"adapter": "omp", "model": "b", "tier": "balanced"},
            },
        }
    )
    async with httpx.AsyncClient() as client:
        routers = build_routers(settings, client)
        decision, _ = await decide_with_fallback(
            DispatchRequest(task="specified", cwd=str(tmp_path), capability="fast"),
            settings.enabled_executors(),
            settings,
            routers,
        )
    assert decision.executor == "fast"


@pytest.mark.asyncio
@respx.mock
async def test_untrusted_jev_falls_back_through_decide_with_fallback(tmp_path: Path) -> None:
    # Отказ preflight живёт в JevRouter: путь dispatch получает RouterError и
    # обычный fallback, не дойдя до сети.
    route = respx.post("https://router.invalid/decide").respond(200, json={})
    settings = Settings.model_validate(
        {
            "router": {"backend": "jev", "jev": {"base_url": "https://router.invalid/decide"}},
            "routing": {"fallback_executor": "balanced"},
            # Два кандидата: с одним decide_with_fallback не зовёт роутер вовсе.
            "executors": {
                "balanced": {"adapter": "omp", "model": "m", "tier": "balanced"},
                "strong": {"adapter": "claude", "tier": "strong"},
            },
            "secrets": {"OPENROUTER_API_KEY": "test-key"},  # pragma: allowlist secret
        }
    )
    async with httpx.AsyncClient() as client:
        decision, events = await decide_with_fallback(
            DispatchRequest(task="unknown project", cwd=str(tmp_path)),
            settings.enabled_executors(),
            settings,
            [JevRouter(settings, client)],
        )
    assert not route.called
    assert decision.router == RouterKind.fallback
    assert any("external router denied" in event.detail for event in events)
