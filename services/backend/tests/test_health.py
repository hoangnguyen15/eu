import asyncio
from collections.abc import Iterator

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


async def _ok(_: Request) -> None:
    return None


async def _boom(_: Request) -> None:
    raise ConnectionError("password=hunter2 host=10.0.0.5")


async def _hang(_: Request) -> None:
    await asyncio.sleep(10)


@pytest.fixture
def make_client() -> Iterator:
    clients: list[TestClient] = []

    def _make(checks: dict | None = None, **overrides: object) -> TestClient:
        settings = Settings(app_env="test", health_check_timeout_seconds=0.2, **overrides)
        client = TestClient(create_app(settings))
        client.__enter__()  # runs lifespan; engine/redis connect lazily
        if checks is not None:
            client.app.state.health_checks = checks  # type: ignore[attr-defined]
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def test_live_does_not_touch_dependencies(make_client) -> None:
    client = make_client(checks={"database": _boom})
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_ok_when_all_checks_pass(make_client) -> None:
    client = make_client(checks={"database": _ok, "redis": _ok})
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_ready_503_and_no_detail_leak_on_failure(make_client) -> None:
    client = make_client(checks={"database": _boom, "redis": _ok})
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"] == {"database": "error:ConnectionError", "redis": "ok"}
    assert "hunter2" not in resp.text and "10.0.0.5" not in resp.text


def test_ready_503_on_timeout(make_client) -> None:
    client = make_client(checks={"redis": _hang})
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"] == "error:TimeoutError"


def test_ready_503_with_unreachable_real_dependencies(make_client) -> None:
    # Default checks against nothing listening: must degrade to 503, not 500.
    client = make_client(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/x", redis_url="redis://127.0.0.1:1/0"
    )
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert set(resp.json()["checks"]) == {"database", "redis"}


def test_request_id_generated_and_echoed(make_client) -> None:
    client = make_client(checks={})
    generated = client.get("/health/live").headers["X-Request-ID"]
    assert len(generated) == 36
    echoed = client.get("/health/live", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"]
    assert echoed == "abc-123"
