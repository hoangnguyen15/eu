"""Integration: auth, generations, credits, idempotency, isolation."""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from tests.conftest import register_and_login

pytestmark = pytest.mark.integration

GEN = {"prompt": "A cinematic futuristic city", "aspect_ratio": "16:9", "number_of_images": 1,
       "reference_images": [], "metadata": {"source": "mobile", "client_version": "1.0.0"}}


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["checks"] == {"database": True, "redis": True, "storage": True}
    assert body["data"]["providers"]["google_flow"] is False
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_register_login_profile(client: httpx.AsyncClient) -> None:
    u = await register_and_login(client)
    r = await client.get("/api/v1/user/profile", headers=u["headers"])
    assert r.status_code == 200 and r.json()["data"]["email"] == u["email"]
    r = await client.get("/api/v1/user/credits", headers=u["headers"])
    assert r.json()["data"] == {"balance": 10, "reserved_balance": 0, "available_balance": 10}


async def test_register_duplicate_and_validation(client: httpx.AsyncClient) -> None:
    u = await register_and_login(client)
    r = await client.post("/api/v1/auth/register", json={"email": u["email"].upper(), "password": "another-password"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "EMAIL_TAKEN"
    r = await client.post("/api/v1/auth/register", json={"email": "bad", "password": "x"})
    assert r.status_code == 422 and r.json()["success"] is False
    r = await client.post("/api/v1/auth/register", json={"email": "x@example.com", "password": "longenough1",
                                                         "role": "admin"})
    assert r.status_code == 422  # extra fields forbidden: no privilege escalation via body


async def test_login_failures(client: httpx.AsyncClient) -> None:
    u = await register_and_login(client)
    r = await client.post("/api/v1/auth/login", json={"email": u["email"], "password": "wrong-password"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    r = await client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever-123"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    r = await client.get("/api/v1/user/profile")
    assert r.status_code == 401
    r = await client.get("/api/v1/user/profile", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_refresh_rotation_and_reuse_detection(client: httpx.AsyncClient) -> None:
    u = await register_and_login(client)
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": u["refresh_token"]})
    assert r1.status_code == 200
    new = r1.json()["data"]
    assert new["refresh_token"] != u["refresh_token"]
    # Reusing the rotated token is detected and revokes the whole family.
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": u["refresh_token"]})
    assert r2.status_code == 401 and r2.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"
    r3 = await client.post("/api/v1/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r3.status_code == 401


async def test_logout_revokes_refresh(client: httpx.AsyncClient) -> None:
    u = await register_and_login(client)
    r = await client.post("/api/v1/auth/logout", json={"refresh_token": u["refresh_token"]}, headers=u["headers"])
    assert r.status_code == 200
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": u["refresh_token"]})
    assert r.status_code == 401


async def test_create_generation_reserves_credits(client: httpx.AsyncClient, user: dict) -> None:
    r = await client.post("/api/v1/generations", json={**GEN, "number_of_images": 3}, headers=user["headers"])
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["status"] == "queued" and uuid.UUID(data["generation_id"])
    c = (await client.get("/api/v1/user/credits", headers=user["headers"])).json()["data"]
    assert c == {"balance": 10, "reserved_balance": 3, "available_balance": 7}
    tx = (await client.get("/api/v1/credits/transactions", headers=user["headers"])).json()["data"]
    assert [t["transaction_type"] for t in tx][:2] == ["RESERVE", "GRANT"]


async def test_idempotency_key_replay_and_conflict(client: httpx.AsyncClient, user: dict) -> None:
    h = {**user["headers"], "Idempotency-Key": f"idem-{uuid.uuid4().hex}"}
    r1 = await client.post("/api/v1/generations", json=GEN, headers=h)
    r2 = await client.post("/api/v1/generations", json=GEN, headers=h)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.headers.get("idempotent-replayed") == "true"
    assert r1.json()["data"]["generation_id"] == r2.json()["data"]["generation_id"]
    c = (await client.get("/api/v1/user/credits", headers=user["headers"])).json()["data"]
    assert c["reserved_balance"] == 1  # charged once
    r3 = await client.post("/api/v1/generations", json={**GEN, "prompt": "different"}, headers=h)
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_idempotency_under_concurrent_retries(client: httpx.AsyncClient, user: dict) -> None:
    h = {**user["headers"], "Idempotency-Key": f"race-{uuid.uuid4().hex}"}
    rs = await asyncio.gather(*[client.post("/api/v1/generations", json=GEN, headers=h) for _ in range(8)])
    assert all(r.status_code in (200, 201) for r in rs), [r.text for r in rs]
    assert len({r.json()["data"]["generation_id"] for r in rs}) == 1
    c = (await client.get("/api/v1/user/credits", headers=user["headers"])).json()["data"]
    assert c["reserved_balance"] == 1


async def test_concurrent_creates_cannot_overspend(client: httpx.AsyncClient, user: dict) -> None:
    rs = await asyncio.gather(*[client.post("/api/v1/generations", json={**GEN, "number_of_images": 3},
                                            headers=user["headers"]) for _ in range(6)])
    codes = sorted(r.status_code for r in rs)
    assert codes.count(201) == 3 and codes.count(402) == 3, codes
    c = (await client.get("/api/v1/user/credits", headers=user["headers"])).json()["data"]
    assert c == {"balance": 10, "reserved_balance": 9, "available_balance": 1}
    r = await client.post("/api/v1/generations", json={**GEN, "number_of_images": 2}, headers=user["headers"])
    assert r.status_code == 402 and r.json()["error"]["code"] == "INSUFFICIENT_CREDITS"


async def test_generation_validation(client: httpx.AsyncClient, user: dict) -> None:
    h = user["headers"]
    for body, code in [
        ({**GEN, "aspect_ratio": "7:3"}, "VALIDATION_ERROR"),
        ({**GEN, "number_of_images": 9}, "VALIDATION_ERROR"),
        ({**GEN, "prompt": ""}, "VALIDATION_ERROR"),
        ({**GEN, "provider": "google_flow"}, "PROVIDER_DISABLED"),
        ({**GEN, "reference_images": [str(uuid.uuid4())]}, "REFERENCE_NOT_FOUND"),
    ]:
        r = await client.post("/api/v1/generations", json=body, headers=h)
        assert r.status_code in (400, 404, 422) and r.json()["error"]["code"] == code, (body, r.text)
    r = await client.post("/api/v1/generations", content=b"x" * 2_000_000,
                          headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 413


async def test_cancel_queued_generation_refunds(client: httpx.AsyncClient, user: dict) -> None:
    r = await client.post("/api/v1/generations", json={**GEN, "number_of_images": 2}, headers=user["headers"])
    gid = r.json()["data"]["generation_id"]
    r = await client.post(f"/api/v1/generations/{gid}/cancel", headers=user["headers"])
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
    assert r.json()["data"]["credit_state"] == "released"
    c = (await client.get("/api/v1/user/credits", headers=user["headers"])).json()["data"]
    assert c == {"balance": 10, "reserved_balance": 0, "available_balance": 10}
    r = await client.post(f"/api/v1/generations/{gid}/cancel", headers=user["headers"])  # idempotent
    assert r.status_code == 200
    tx = (await client.get("/api/v1/credits/transactions", headers=user["headers"])).json()["data"]
    assert sum(1 for t in tx if t["transaction_type"] == "RELEASE") == 1


async def test_user_isolation(client: httpx.AsyncClient, user: dict) -> None:
    other = await register_and_login(client)
    r = await client.post("/api/v1/generations", json=GEN, headers=user["headers"])
    gid = r.json()["data"]["generation_id"]
    assert (await client.get(f"/api/v1/generations/{gid}", headers=other["headers"])).status_code == 404
    assert (await client.post(f"/api/v1/generations/{gid}/cancel", headers=other["headers"])).status_code == 404
    lst = (await client.get("/api/v1/generations", headers=other["headers"])).json()["data"]
    assert lst["items"] == []
    assert (await client.get(f"/api/v1/assets/{uuid.uuid4()}", headers=other["headers"])).status_code == 404


async def test_list_pagination(client: httpx.AsyncClient, user: dict) -> None:
    ids = []
    for i in range(5):
        r = await client.post("/api/v1/generations", json={**GEN, "prompt": f"p{i}"}, headers=user["headers"])
        ids.append(r.json()["data"]["generation_id"])
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/api/v1/generations", params=params, headers=user["headers"])).json()["data"]
        seen += [g["id"] for g in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == list(reversed(ids))


async def test_admin_endpoints_require_admin_role(client: httpx.AsyncClient, user: dict) -> None:
    r = await client.post("/api/v1/admin/enrollment-tokens", json={"workstation_name": "x"}, headers=user["headers"])
    assert r.status_code == 403
    r = await client.get("/api/v1/admin/workstations", headers=user["headers"])
    assert r.status_code == 403


async def test_admin_grant_credits_and_audit(client: httpx.AsyncClient, user: dict, admin: dict) -> None:
    r = await client.post("/api/v1/admin/credits/grant", json={"user_id": user["id"], "amount": 5},
                          headers=admin["headers"])
    assert r.status_code == 200 and r.json()["data"]["balance"] == 15
    logs = (await client.get("/api/v1/admin/audit-logs", params={"resource_type": "user"},
                             headers=admin["headers"])).json()["data"]
    assert any(e["action"] == "credits.grant" and e["resource_id"] == user["id"] for e in logs)


async def test_rate_limit_enforced() -> None:
    import os

    from app.core import redis as redis_mod
    from app.core.config import get_settings
    from app.core.deps import hit_rate_limit
    from app.core.exceptions import RateLimitedError

    get_settings.cache_clear()
    redis_mod._redis = None
    os.environ["RATE_LIMIT_ENABLED"] = "true"
    ident = uuid.uuid4().hex
    for _ in range(3):
        await hit_rate_limit("unit", ident, 3)
    with pytest.raises(RateLimitedError) as e:
        await hit_rate_limit("unit", ident, 3)
    assert "Retry-After" in (e.value.headers or {})
    await redis_mod.close_redis()
