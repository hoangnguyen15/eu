"""Integration: admin console API and bundled web UIs."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.conftest import ensure_account, register_and_login, wait_for, wait_status
from tests.test_worker_flow import GEN, TERMINAL, _async_true, running_agent

pytestmark = pytest.mark.integration


async def test_overview_jobs_sessions_and_session_check(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, capacity=3) as agent:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_status(client, u["headers"], gid, TERMINAL)

        ov = (await client.get("/api/v1/admin/overview", headers=admin["headers"])).json()["data"]
        assert ov["workstations"]["ONLINE"]["count"] >= 1 and ov["slots"]["online_capacity"] >= 3
        assert ov["last_24h"]["completed"]["count"] >= 1 and ov["last_24h"]["completed"]["p95_seconds"] > 0

        jobs = (await client.get("/api/v1/admin/jobs?status=COMPLETED&limit=50",
                                 headers=admin["headers"])).json()["data"]
        job = next(j for j in jobs if j["id"] == gid)
        assert job["user_email"] == u["email"] and job["worker_name"] and job["account_email"] == agent.account
        assert job["duration_seconds"] > 0 and job["assets"] and job["assets"][0]["thumbnail_url"]

        sessions = (await client.get("/api/v1/admin/sessions", headers=admin["headers"])).json()["data"]
        mine = [s for s in sessions if s["workstation_id"] == agent.workstation_id]
        assert mine and mine[0]["session_status"] == "ACTIVE" and mine[0]["google_email"] == agent.account
        before = mine[0]["last_verified_at"]

        r = await client.post(f"/api/v1/admin/workstations/{agent.workstation_id}/session-check",
                              headers=admin["headers"])
        assert r.status_code == 200 and r.json()["data"]["online"] is True
        await wait_for(lambda: _async_true(agent.session_checks == 1), 5)

        async def reverified() -> bool:
            rows = (await client.get("/api/v1/admin/sessions", headers=admin["headers"])).json()["data"]
            return any(s["workstation_id"] == agent.workstation_id and s["last_verified_at"] > before for s in rows)
        await wait_for(reverified, 5)

        # disable -> enable round trip
        assert (await client.post(f"/api/v1/admin/workstations/{agent.workstation_id}/disable",
                                  headers=admin["headers"])).status_code == 200
        r = await client.post(f"/api/v1/admin/workstations/{agent.workstation_id}/enable", headers=admin["headers"])
        assert r.json()["data"]["status"] == "OFFLINE"


async def test_provision_requires_authorized_accounts(client: httpx.AsyncClient, admin: dict) -> None:
    r = await client.post("/api/v1/admin/workstations/provision", headers=admin["headers"], json={
        "workstation_name": "WIN-X", "capacity": 2,
        "accounts": [{"google_email": f"nobody-{uuid.uuid4().hex[:6]}@example.com", "profile_ref": "p1"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ACCOUNT_NOT_AUTHORIZED"

    acct = f"prov-{uuid.uuid4().hex[:6]}@example.com"
    await ensure_account(client, admin, acct)
    r = await client.post("/api/v1/admin/workstations/provision", headers=admin["headers"], json={
        "workstation_name": "WIN-Y", "capacity": 3,
        "accounts": [{"google_email": acct, "profile_ref": "profile-1", "provider": "mock"}]})
    assert r.status_code == 201, r.text
    cfg = r.json()["data"]["config"]
    assert cfg["ServerBaseUrl"].startswith("https://") and cfg["Capacity"] == 3
    assert "BEGIN CERTIFICATE" in cfg["CaCertificatePem"] and len(cfg["EnrollmentToken"]) > 20
    assert cfg["Accounts"] == [{"GoogleEmail": acct, "ProfileRef": "profile-1", "Provider": "mock"}]
    json.dumps(cfg)  # serialisable as the installer's config file

    # the token in the bundle really enrolls an agent (single use)
    from tests.conftest import make_csr
    csr, _ = make_csr()
    r1 = await client.post("/api/v1/agent/enroll", json={"token": cfg["EnrollmentToken"], "csr_pem": csr})
    r2 = await client.post("/api/v1/agent/enroll", json={"token": cfg["EnrollmentToken"], "csr_pem": csr})
    assert r1.status_code == 200 and r2.status_code == 401


async def test_admin_console_endpoints_require_admin(client: httpx.AsyncClient, user: dict) -> None:
    for method, path in [("GET", "/api/v1/admin/overview"), ("GET", "/api/v1/admin/jobs"),
                         ("GET", "/api/v1/admin/sessions"), ("POST", "/api/v1/admin/workstations/provision")]:
        r = await client.request(method, path, headers=user["headers"], json={} if method == "POST" else None)
        assert r.status_code == 403, (path, r.status_code)


async def test_web_uis_are_served_with_csp(client: httpx.AsyncClient) -> None:
    for path, marker in [("/app/", "client.js"), ("/admin/", "admin.js"), ("/shared/api.js", "EventStream")]:
        r = await client.get(path)
        assert r.status_code == 200 and marker in r.text, path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/"
