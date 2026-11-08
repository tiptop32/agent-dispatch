"""Preflight checks before a router can receive request state."""

from __future__ import annotations

from pathlib import Path

from agent_dispatch.config import Settings
from agent_dispatch.models import DispatchRequest

EXTERNAL_ROUTER_DENIED = "external router denied: cwd is not a configured trusted project"


def _real_directory(value: str | Path) -> Path | None:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_dir() else None


def external_router_allowed(req: DispatchRequest, settings: Settings) -> bool:
    """Return whether request state may be sent to an external router.

    The default is deny.  A request is trusted only when its resolved cwd is
    contained by a resolved, configured root.  Resolving both sides prevents a
    symlink from escaping (or entering) the configured perimeter.
    """
    if req.prod:
        return False
    policy = settings.router.external
    cwd = _real_directory(req.cwd)
    if cwd is None:
        return False
    if policy.allow_unknown_projects:
        return True
    for configured_root in policy.trusted_roots:
        root = _real_directory(configured_root)
        if root is not None and (cwd == root or root in cwd.parents):
            return True
    return False
