import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(prefix="/health", tags=["health"])

Check = Callable[[Request], Awaitable[None]]


async def check_database(request: Request) -> None:
    async with request.app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_redis(request: Request) -> None:
    await request.app.state.redis.ping()


# Registered by name so tests/other deployments can swap or extend checks.
DEFAULT_CHECKS: dict[str, Check] = {"database": check_database, "redis": check_redis}


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: process is up. Must not touch dependencies."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: all hard dependencies reachable. 503 if any is not."""
    timeout = request.app.state.settings.health_check_timeout_seconds
    checks: dict[str, Check] = request.app.state.health_checks

    async def run(check: Check) -> str:
        try:
            await asyncio.wait_for(check(request), timeout=timeout)
            return "ok"
        except Exception as exc:  # noqa: BLE001 - report type only, never details/secrets
            return f"error:{type(exc).__name__}"

    results = dict(
        zip(checks, await asyncio.gather(*(run(c) for c in checks.values())), strict=True)
    )
    healthy = all(v == "ok" for v in results.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unavailable", "checks": results},
    )
