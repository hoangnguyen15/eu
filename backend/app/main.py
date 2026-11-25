from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1 import admin, agent, assets, auth, credits, generations, websocket
from app.core.config import get_settings
from app.core.db import dispose_engine, session_factory
from app.core.exceptions import error_body, register_exception_handlers
from app.core.logging import configure_logging
from app.core.redis import close_redis, get_redis
from app.providers.registry import provider_states
from app.storage import s3
from app.workers import gateway

log = logging.getLogger(__name__)


class BodySizeLimitMiddleware:
    """Rejects bodies over MAX_REQUEST_BYTES, by Content-Length and while streaming."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                await JSONResponse(error_body("PAYLOAD_TOO_LARGE", "Request body too large"), 413)(
                    scope, receive, send)
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return msg

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await JSONResponse(error_body("PAYLOAD_TOO_LARGE", "Request body too large"), 413)(scope, receive, send)


class _TooLarge(Exception):
    pass


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id" and len(value) <= 64:
                request_id = value.decode(errors="ignore")
        request_id = request_id or uuid.uuid4().hex

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging(s.log_level)
    if s.s3_create_bucket:
        await asyncio.to_thread(s3.ensure_bucket_sync)
    yield
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Google Flow Automation Platform API",
        version="1.0.0",
        description="Central REST/WebSocket API for mobile clients and Windows worker agents.",
        lifespan=lifespan,
        docs_url=None if s.is_production else "/docs",
        redoc_url=None if s.is_production else "/redoc",
        openapi_url="/api/v1/openapi.json",
    )
    register_exception_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=s.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    if s.cors_allow_origins:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_allow_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PATCH"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"])

    api = APIRouter(prefix="/api/v1")
    for r in (auth.router, credits.router, generations.router, assets.router, agent.router, admin.router):
        api.include_router(r)

    @api.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        checks: dict[str, bool] = {}
        try:
            async with session_factory()() as db:
                await db.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            checks["database"] = False
        try:
            checks["redis"] = bool(await get_redis().ping())
        except Exception:
            checks["redis"] = False
        checks["storage"] = await s3.ping()
        healthy = all(checks.values())
        body = {"success": healthy, "data": {"status": "ok" if healthy else "degraded", "checks": checks,
                                            "providers": provider_states()}}
        return JSONResponse(body, status_code=200 if healthy else 503)

    @api.get("/health/live", tags=["health"])
    async def live() -> dict:
        return {"success": True, "data": {"status": "alive"}}

    app.include_router(api)
    app.include_router(websocket.router)
    app.include_router(gateway.router)
    return app


app = create_app()
