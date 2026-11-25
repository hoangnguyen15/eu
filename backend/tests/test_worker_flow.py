"""Integration: enrollment, agent protocol, scheduler, recovery, storage and the
full MockImageProvider pipeline with WebSocket notifications."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import websockets

from tests.agent_sim import SimAgent
from tests.conftest import (
    GATEWAY_SECRET,
    enroll,
    ensure_account,
    make_csr,
    register_and_login,
    wait_for,
    wait_status,
)

pytestmark = pytest.mark.integration

GEN = {"prompt": "A cinematic futuristic city", "aspect_ratio": "16:9", "number_of_images": 1,
       "reference_images": [], "metadata": {"source": "mobile"}}
TERMINAL = {"completed", "failed", "cancelled", "auth_required", "provider_limited", "review_required"}


async def assert_ws_rejected(url: str, headers: dict[str, str] | None = None) -> None:
    """Rejected either at the handshake (HTTP 403) or right after (close 4401)."""
    try:
        ws = await websockets.connect(url, additional_headers=headers or {})
    except websockets.InvalidStatus as exc:
        assert exc.response.status_code == 403
        return
    with pytest.raises(websockets.ConnectionClosed) as e:
        await asyncio.wait_for(ws.recv(), 5)
    assert e.value.rcvd is not None and e.value.rcvd.code == 4401


@pytest.fixture(autouse=True)
async def _isolated_queue(stack: dict) -> None:
    """Jobs left QUEUED by other test modules (no agent online there) would be
    dispatched to this module's agents; cancel them so each test sees only its own."""
    import asyncpg

    from tests.conftest import _test_db_url

    conn = await asyncpg.connect(_test_db_url("postgresql"))
    try:
        await conn.execute("UPDATE generations SET status = 'CANCELLED', error_code = 'TEST_ISOLATION' "
                           "WHERE status = 'QUEUED'")
    finally:
        await conn.close()


@contextlib.asynccontextmanager
async def running_agent(stack: dict, client: httpx.AsyncClient, admin: dict, *, account: str | None = None,
                        **kw: Any) -> Any:
    account = account or f"mock-{uuid.uuid4().hex[:8]}@example.com"
    await ensure_account(client, admin, account)
    info = await enroll(client, admin, name=f"sim-{uuid.uuid4().hex[:6]}")
    agent = SimAgent(ws_url=f"{stack['ws']}/ws/v1/agent", gateway_secret=GATEWAY_SECRET,
                     cert_pem=info["certificate_pem"],
                     sessions=[{"google_email": account, "profile_ref": "profile-1", "provider": "mock",
                                "status": "ACTIVE"}], **kw)
    agent.workstation_id = info["workstation_id"]  # type: ignore[attr-defined]
    agent.account = account  # type: ignore[attr-defined]
    await agent.start()
    try:
        yield agent
    finally:
        await agent.stop()


class EventListener:
    def __init__(self, url: str) -> None:
        self.url = url
        self.events: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()

    async def __aenter__(self) -> EventListener:
        self._ws = await websockets.connect(self.url)
        self._task = asyncio.create_task(self._read())
        await asyncio.wait_for(self.ready.wait(), 5)
        return self

    async def _read(self) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            async for raw in self._ws:
                evt = json.loads(raw)
                if evt.get("event") == "connection.ready":
                    self.ready.set()
                self.events.append(evt)

    async def __aexit__(self, *a: Any) -> None:
        if self._task:
            self._task.cancel()
        await self._ws.close()

    def names(self, gid: str | None = None) -> list[str]:
        return [e["event"] for e in self.events
                if gid is None or e.get("data", {}).get("generation_id") == gid]


# ------------------------------------------------------------ enrollment ----
async def test_enrollment_token_is_single_use(client: httpx.AsyncClient, admin: dict) -> None:
    info = await enroll(client, admin, "single-use")
    csr, _ = make_csr()
    r = await client.post("/api/v1/agent/enroll", json={"token": info["token"], "csr_pem": csr})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_ENROLLMENT_TOKEN"
    assert "BEGIN CERTIFICATE" in info["certificate_pem"] and "BEGIN CERTIFICATE" in info["ca_certificate_pem"]


async def test_agent_socket_rejects_unauthenticated(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    info = await enroll(client, admin, "auth-check")
    url = f"{stack['ws']}/ws/v1/agent"
    good = {"X-Gateway-Secret": GATEWAY_SECRET, "X-Client-Verify": "SUCCESS",
            "X-Client-Cert": quote(info["certificate_pem"])}
    bad_variants = [
        {},  # nothing
        {**good, "X-Gateway-Secret": "wrong-secret-wrong-secret-xx"},  # spoofed proxy
        {**good, "X-Client-Verify": "FAILED:unknown ca"},
        {**good, "X-Client-Cert": quote(info["ca_certificate_pem"])},  # cert not pinned to a workstation
    ]
    for headers in bad_variants:
        await assert_ws_rejected(url, headers)
    # Disabled workstation is rejected even with a valid certificate.
    r = await client.post(f"/api/v1/admin/workstations/{info['workstation_id']}/disable", headers=admin["headers"])
    assert r.status_code == 200
    await assert_ws_rejected(url, good)


async def test_register_heartbeat_and_sessions(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    async with running_agent(stack, client, admin, capacity=3) as agent:
        unknown = await agent.send_event("SESSION_STATUS", {"sessions": [
            {"google_email": "not-registered@example.com", "profile_ref": "p", "provider": "mock",
             "status": "ACTIVE"}]})
        assert unknown["result"]["unregistered"] == ["not-registered@example.com"]

        async def online() -> dict | None:
            rows = (await client.get("/api/v1/admin/workstations", headers=admin["headers"])).json()["data"]
            row = next(w for w in rows if w["id"] == agent.workstation_id)
            return row if row["status"] == "ONLINE" and row["capacity"] == 3 else None
        row = await wait_for(online, 5)
        first = row["last_heartbeat_at"]
        await asyncio.sleep(2.5)
        assert (await online())["last_heartbeat_at"] > first  # heartbeats advance
        accts = (await client.get("/api/v1/admin/google-accounts", headers=admin["headers"])).json()["data"]
        assert next(a for a in accts if a["google_email"] == agent.account)["session_status"] == "ACTIVE"


async def test_duplicate_event_is_acknowledged_idempotently(stack: dict, client: httpx.AsyncClient,
                                                            admin: dict) -> None:
    async with running_agent(stack, client, admin) as agent:
        eid = uuid.uuid4().hex
        sessions = [{"google_email": agent.account, "profile_ref": "profile-1", "provider": "mock",
                     "status": "ACTIVE"}]
        a1 = await agent.send_event("SESSION_STATUS", {"sessions": sessions}, event_id=eid)
        a2 = await agent.send_event("SESSION_STATUS", {"sessions": sessions}, event_id=eid)
        assert a1["duplicate"] is False and a2["duplicate"] is True and a1["result"] == a2["result"]


# --------------------------------------------------------- end-to-end ----
async def test_end_to_end_mock_pipeline(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    """Mobile API -> scheduler -> agent -> provider -> signed upload -> verification
    -> storage -> REST result -> WebSocket notifications, with credits settled."""
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, capacity=2) as agent, \
            EventListener(f"{stack['ws']}/ws/v1/events?access_token={u['access_token']}") as ev:
        r = await client.post("/api/v1/generations", json={**GEN, "number_of_images": 2},
                              headers={**u["headers"], "Idempotency-Key": f"e2e-{uuid.uuid4().hex}"})
        assert r.status_code == 201
        gid = r.json()["data"]["generation_id"]
        gen = await wait_status(client, u["headers"], gid, TERMINAL)
        assert gen["status"] == "completed", gen
        assert gen["progress"] == 100 and gen["credit_state"] == "settled"
        assert gen["provider_metadata"]["model"] == "mock-image-v1"
        assert len(gen["assets"]) == 2
        assert sum(1 for o in agent.offers if o["generation_id"] == gid) == 1

        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as raw:
            for asset in gen["assets"]:
                assert asset["mime_type"] == "image/png" and (asset["width"], asset["height"]) == (768, 432)
                blob = (await raw.get(asset["url"])).content  # signed URL on private bucket
                assert hashlib.sha256(blob).hexdigest() == asset["sha256"]
                thumb = await raw.get(asset["thumbnail_url"])
                assert thumb.status_code == 200 and thumb.content[8:12] == b"WEBP"
                # download endpoint -> 307 to signed URL; unsigned object URL is denied (private bucket)
                d = await client.get(f"/api/v1/assets/{asset['id']}/download", headers=u["headers"])
                assert d.status_code == 307 and "X-Amz-Signature" in d.headers["location"]
                unsigned = asset["url"].split("?")[0]
                assert (await raw.get(unsigned)).status_code == 403
                meta = await client.get(f"/api/v1/assets/{asset['id']}", headers=u["headers"])
                assert meta.status_code == 200

        credits = (await client.get("/api/v1/user/credits", headers=u["headers"])).json()["data"]
        assert credits == {"balance": 8, "reserved_balance": 0, "available_balance": 8}

        await wait_for(lambda: _async_true("generation.completed" in ev.names(gid)), 5)
        names = ev.names(gid)
        for expected in ("generation.queued", "generation.started", "generation.progress", "generation.completed"):
            assert expected in names, names
        assert names.index("generation.queued") < names.index("generation.started") < names.index(
            "generation.completed")
        done = next(e for e in ev.events if e["event"] == "generation.completed")
        assert set(done) >= {"event_id", "event", "timestamp", "data"} and len(done["data"]["asset_ids"]) == 2
        assert any(e["event"] == "credits.updated" and e["data"]["balance"] == 8 for e in ev.events)


async def _async_true(v: bool) -> bool:
    return v


async def test_websocket_auth_and_replay(stack: dict, client: httpx.AsyncClient) -> None:
    u = await register_and_login(client)
    await assert_ws_rejected(f"{stack['ws']}/ws/v1/events?access_token=garbage")

    async with EventListener(f"{stack['ws']}/ws/v1/events?access_token={u['access_token']}") as ev:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_for(lambda: _async_true("generation.queued" in ev.names(gid)), 5)
        cursor = next(e for e in ev.events if e.get("event") == "generation.queued")["cursor"]
        await ev._ws.send(json.dumps({"type": "ping"}))
        await wait_for(lambda: _async_true("pong" in ev.names()), 5)
    # While disconnected, more events happen; reconnecting with ?since= replays them.
    await client.post(f"/api/v1/generations/{gid}/cancel", headers=u["headers"])
    headers = {"Authorization": f"Bearer {u['access_token']}"}
    ws = await websockets.connect(f"{stack['ws']}/ws/v1/events?since={cursor}", additional_headers=headers)
    got = []
    with contextlib.suppress(asyncio.TimeoutError):
        while True:
            got.append(json.loads(await asyncio.wait_for(ws.recv(), 2))["event"])
            if "generation.cancelled" in got and "credits.updated" in got:
                break
    await ws.close()
    assert "generation.cancelled" in got and "generation.queued" not in got


# -------------------------------------------------------- failure paths ----
@pytest.mark.parametrize("token,status,refunded", [
    ("[mock:fail]", "failed", True),
    ("[mock:auth]", "auth_required", True),
    ("[mock:limit]", "provider_limited", True),
    ("[mock:review]", "review_required", False),
])
async def test_provider_failures_map_to_status_and_refund(stack: dict, client: httpx.AsyncClient, admin: dict,
                                                          token: str, status: str, refunded: bool) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin) as agent:
        r = await client.post("/api/v1/generations", json={**GEN, "prompt": f"city {token}"}, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        gen = await wait_status(client, u["headers"], gid, TERMINAL)
        assert gen["status"] == status and gen["assets"] == []
        c = (await client.get("/api/v1/user/credits", headers=u["headers"])).json()["data"]
        if refunded:
            assert c == {"balance": 10, "reserved_balance": 0, "available_balance": 10}
            assert gen["credit_state"] == "released"
        else:
            assert c["reserved_balance"] == 1 and gen["credit_state"] == "reserved"
            rr = await client.post(f"/api/v1/admin/generations/{gid}/resolve", json={"action": "refund"},
                                   headers=admin["headers"])
            assert rr.status_code == 200 and rr.json()["data"]["credit_state"] == "RELEASED"
        accts = {a["google_email"]: a for a in
                 (await client.get("/api/v1/admin/google-accounts", headers=admin["headers"])).json()["data"]}
        if status == "provider_limited":
            # The account is marked LIMITED; the job is NOT moved to another account.
            assert accts[agent.account]["status"] == "LIMITED"
            assert len(agent.offers) == 1
        if status == "auth_required":
            assert accts[agent.account]["session_status"] == "AUTH_REQUIRED"


async def test_corrupted_upload_never_completes(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, corrupt_upload=True):
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        gen = await wait_status(client, u["headers"], gid, TERMINAL)
        assert gen["status"] == "failed" and gen["error_code"] == "ASSET_VERIFICATION_FAILED"
        assert gen["error_message"] == "CHECKSUM_MISMATCH" and gen["credit_state"] == "released"


async def test_task_completed_rejected_before_assets_verified(stack: dict, client: httpx.AsyncClient,
                                                             admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, execute=False) as agent:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_for(lambda: _async_true(bool(agent.offers)), 10)
        task_id = agent.offers[0]["task_id"]
        await wait_for(lambda: _async_true(any(a["status"] == "ok" for a in agent.acks.values()
                                               if a.get("result", {}).get("lease_expires_at"))), 5)
        assert (await agent.send_event("TASK_STARTED", {"task_id": task_id}))["status"] == "ok"
        ack = await agent.send_event("TASK_COMPLETED", {"task_id": task_id, "provider": "mock"})
        assert ack["status"] == "rejected" and ack["result"]["reason"] == "LEASE_NOT_UPLOADING"
        gen = (await client.get(f"/api/v1/generations/{gid}", headers=u["headers"])).json()["data"]
        assert gen["status"] == "running"
        # A foreign agent cannot act on this task.
        async with running_agent(stack, client, admin, execute=False) as other:
            nack = await other.send_event("TASK_FAILED", {"task_id": task_id, "error_code": "X"})
            assert nack["status"] == "rejected" and nack["result"]["reason"] == "UNKNOWN_TASK"


# ----------------------------------------------------- scheduler / recovery ----
async def test_many_jobs_across_workers_no_double_dispatch(stack: dict, client: httpx.AsyncClient,
                                                           admin: dict) -> None:
    u = await register_and_login(client)
    await client.post("/api/v1/admin/credits/grant", json={"user_id": (await client.get(
        "/api/v1/user/profile", headers=u["headers"])).json()["data"]["id"], "amount": 50}, headers=admin["headers"])
    async with running_agent(stack, client, admin, capacity=2) as a1, \
            running_agent(stack, client, admin, capacity=2) as a2, \
            running_agent(stack, client, admin, capacity=1) as a3:
        rs = await asyncio.gather(*[client.post("/api/v1/generations", json={**GEN, "prompt": f"job {i}"},
                                                headers=u["headers"]) for i in range(12)])
        gids = [r.json()["data"]["generation_id"] for r in rs]
        gens = await asyncio.gather(*[wait_status(client, u["headers"], g, TERMINAL, timeout=60) for g in gids])
        assert all(g["status"] == "completed" for g in gens), [g["status"] for g in gens]
        task_ids = [o for o in a1.offers + a2.offers + a3.offers if o["generation_id"] in gids]
        per_gen: dict[str, int] = {}
        for o in task_ids:
            per_gen[o["generation_id"]] = per_gen.get(o["generation_id"], 0) + 1
        assert all(v == 1 for v in per_gen.values()) and len(per_gen) == 12
        assert all(any(o["generation_id"] in gids for o in a.offers) for a in (a1, a2, a3))  # load spread
        c = (await client.get("/api/v1/user/credits", headers=u["headers"])).json()["data"]
        assert c == {"balance": 48, "reserved_balance": 0, "available_balance": 48}


async def test_offer_not_accepted_is_requeued_to_another_worker(stack: dict, client: httpx.AsyncClient,
                                                                admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, auto_accept=False) as silent:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_for(lambda: _async_true(bool(silent.offers)), 10)
        await silent.stop()  # goes away without accepting
        async with running_agent(stack, client, admin) as healthy:
            gen = await wait_status(client, u["headers"], gid, TERMINAL, timeout=30)
            assert gen["status"] == "completed" and len(healthy.completed) == 1


async def test_worker_lost_mid_task_fails_without_retry(stack: dict, client: httpx.AsyncClient,
                                                        admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, execute=False) as agent:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_for(lambda: _async_true(bool(agent.offers)), 10)
        task_id = agent.offers[0]["task_id"]
        await wait_for(lambda: _async_true(any(a.get("result", {}).get("lease_expires_at")
                                               for a in agent.acks.values())), 5)
        await agent.send_event("TASK_STARTED", {"task_id": task_id})
        await agent.kill()  # crash while the provider call is in flight
    async with running_agent(stack, client, admin) as standby:
        gen = await wait_status(client, u["headers"], gid, TERMINAL, timeout=30)
        assert gen["status"] == "failed" and gen["error_code"] == "WORKER_LOST"
        assert gen["credit_state"] == "released"
        assert standby.offers == []  # outcome unknown -> not retried elsewhere

        async def offline() -> bool:
            rows = (await client.get("/api/v1/admin/workstations", headers=admin["headers"])).json()["data"]
            return next(w for w in rows if w["id"] == agent.workstation_id)["status"] == "OFFLINE"
        await wait_for(offline, 10)


async def test_cancel_dispatched_offer_revokes_task(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, auto_accept=False) as agent:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_for(lambda: _async_true(bool(agent.offers)), 10)
        c = await client.post(f"/api/v1/generations/{gid}/cancel", headers=u["headers"])
        assert c.status_code == 200 and c.json()["data"]["status"] == "cancelled"
        await wait_for(lambda: _async_true(bool(agent.cancels)), 5)
        ack = await agent.send_event("TASK_ACCEPT", {"task_id": agent.offers[0]["task_id"]})
        assert ack["status"] == "rejected"


async def test_agent_returning_task_before_start_is_requeued(stack: dict, client: httpx.AsyncClient,
                                                             admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, execute=False) as agent:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_for(lambda: _async_true(bool(agent.offers)), 10)
        first = agent.offers[0]["task_id"]
        await wait_for(lambda: _async_true(any(a.get("result", {}).get("lease_expires_at")
                                               for a in agent.acks.values())), 5)
        ack = await agent.send_event("TASK_FAILED", {"task_id": first, "error_code": "NOT_STARTED"})
        assert ack["status"] == "ok" and ack["result"]["generation_status"] == "queued"
        await wait_for(lambda: _async_true(len(agent.offers) >= 2), 10)
        assert agent.offers[1]["generation_id"] == gid and agent.offers[1]["task_id"] != first
        assert agent.offers[1]["attempt"] == 2


async def test_outcome_unknown_goes_to_review(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    u = await register_and_login(client)
    async with running_agent(stack, client, admin, execute=False) as agent:
        r = await client.post("/api/v1/generations", json=GEN, headers=u["headers"])
        gid = r.json()["data"]["generation_id"]
        await wait_for(lambda: _async_true(bool(agent.offers)), 10)
        task_id = agent.offers[0]["task_id"]
        await wait_for(lambda: _async_true(any(a.get("result", {}).get("lease_expires_at")
                                               for a in agent.acks.values())), 5)
        await agent.send_event("TASK_STARTED", {"task_id": task_id})
        # NOT_STARTED after the server saw TASK_STARTED is treated as unknown outcome.
        await agent.send_event("TASK_FAILED", {"task_id": task_id, "error_code": "NOT_STARTED"})
        gen = await wait_status(client, u["headers"], gid, TERMINAL, timeout=10)
        assert gen["status"] == "review_required" and gen["error_code"] == "OUTCOME_UNKNOWN"
        assert len(agent.offers) == 1


async def test_fleet_of_ten_workers(stack: dict, client: httpx.AsyncClient, admin: dict) -> None:
    """>= 10 concurrent workers, 40 jobs from several users: every job completes
    exactly once, no slot is double-booked, credits settle per user."""
    users = [await register_and_login(client) for _ in range(4)]
    for u in users:
        uid = (await client.get("/api/v1/user/profile", headers=u["headers"])).json()["data"]["id"]
        await client.post("/api/v1/admin/credits/grant", json={"user_id": uid, "amount": 10}, headers=admin["headers"])
    async with contextlib.AsyncExitStack() as stack_cm:
        agents = [await stack_cm.enter_async_context(running_agent(stack, client, admin, capacity=2))
                  for _ in range(10)]
        reqs = [client.post("/api/v1/generations", json={**GEN, "prompt": f"fleet {i}"},
                            headers=users[i % 4]["headers"]) for i in range(40)]
        rs = await asyncio.gather(*reqs)
        assert all(r.status_code == 201 for r in rs), [r.text for r in rs if r.status_code != 201]
        jobs = [(users[i % 4], r.json()["data"]["generation_id"]) for i, r in enumerate(rs)]
        gens = await asyncio.gather(*[wait_status(client, u["headers"], g, TERMINAL, timeout=90) for u, g in jobs])
        assert [g["status"] for g in gens] == ["completed"] * 40
        gids = {g for _, g in jobs}
        offers = [o for a in agents for o in a.offers if o["generation_id"] in gids]
        assert len(offers) == 40 and len({o["generation_id"] for o in offers}) == 40  # no double dispatch
        busy = sum(1 for a in agents if any(o["generation_id"] in gids for o in a.offers))
        assert busy >= 8, f"only {busy}/10 workers received work"
        for u in users:
            c = (await client.get("/api/v1/user/credits", headers=u["headers"])).json()["data"]
            assert c == {"balance": 10, "reserved_balance": 0, "available_balance": 10}
