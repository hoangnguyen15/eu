"""End-to-end verification against a running stack (docker compose + FlowAgent).

Acts as the Flutter app (REST + WebSocket) and as an operator (admin API) and
asserts the whole pipeline:
  Mobile API creates job -> scheduler dispatches -> agent receives job ->
  provider renders -> signed upload -> server verification -> storage ->
  REST returns result -> WebSocket notifies; credits settled exactly once.

Exit code 0 only if every check passes. Usage:
  python scripts/e2e_client.py --api http://localhost:8000 --admin-email ... --admin-password ...
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from typing import Any

import httpx
import websockets

TERMINAL = {"completed", "failed", "cancelled", "auth_required", "provider_limited", "review_required"}


class Check:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def __call__(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.results.append((name, bool(ok), str(detail)[:300]))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.results)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--admin-email", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--images", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()
    ws_base = a.api.replace("http://", "ws://").replace("https://", "wss://")
    check = Check()

    async with httpx.AsyncClient(base_url=a.api, timeout=30) as http:
        r = await http.post("/api/v1/auth/login", json={"email": a.admin_email, "password": a.admin_password})
        check("admin login", r.status_code == 200, r.text)
        admin = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}

        # 1. A real .NET agent is enrolled, ONLINE and reporting an authorized session.
        deadline = time.time() + a.timeout
        agent = None
        while time.time() < deadline:
            rows = (await http.get("/api/v1/admin/workstations", headers=admin)).json()["data"]
            online = [w for w in rows if w["status"] == "ONLINE" and w["agent_version"]
                      and not w["agent_version"].startswith("sim")]
            accts = (await http.get("/api/v1/admin/google-accounts", headers=admin)).json()["data"]
            if online and any(x["session_status"] == "ACTIVE" for x in accts):
                agent = online[0]
                break
            await asyncio.sleep(1)
        if not check("FlowAgent (.NET) registered ONLINE via mTLS gateway", agent is not None):
            return 1
        print(f"      workstation={agent['id']} version={agent['agent_version']} capacity={agent['capacity']}")

        # 2. Mobile user.
        email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
        r = await http.post("/api/v1/auth/register", json={"email": email, "password": "e2e-password-123"})
        check("mobile register", r.status_code == 201, r.text)
        r = await http.post("/api/v1/auth/login", json={"email": email, "password": "e2e-password-123"})
        tokens = r.json()["data"]
        user = {"Authorization": f"Bearer {tokens['access_token']}"}
        before = (await http.get("/api/v1/user/credits", headers=user)).json()["data"]

        events: list[dict[str, Any]] = []
        ws = await websockets.connect(f"{ws_base}/ws/v1/events", additional_headers=user)
        ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
        check("websocket connected (JWT)", ready.get("event") == "connection.ready", ready)

        async def reader() -> None:
            try:
                async for raw in ws:
                    events.append(json.loads(raw))
            except websockets.ConnectionClosed:
                pass
        reader_task = asyncio.create_task(reader())

        # 3. Create generation (with a mobile-style retry using the same Idempotency-Key).
        body = {"prompt": "A cinematic futuristic city", "aspect_ratio": "16:9", "number_of_images": a.images,
                "reference_images": [], "metadata": {"source": "mobile", "client_version": "1.0.0"}}
        idem = {**user, "Idempotency-Key": f"e2e-{uuid.uuid4().hex}"}
        r1 = await http.post("/api/v1/generations", json=body, headers=idem)
        r2 = await http.post("/api/v1/generations", json=body, headers=idem)
        check("POST /generations -> 201 queued", r1.status_code == 201 and r1.json()["data"]["status"] == "queued",
              r1.text)
        gid = r1.json()["data"]["generation_id"]
        check("retry with same Idempotency-Key returns same job",
              r2.status_code == 200 and r2.json()["data"]["generation_id"] == gid, r2.text)

        # 4. Wait for the terminal state via REST (authoritative).
        gen: dict[str, Any] = {}
        seen_status: list[str] = []
        t0 = time.time()
        while time.time() < deadline:
            gen = (await http.get(f"/api/v1/generations/{gid}", headers=user)).json()["data"]
            if not seen_status or seen_status[-1] != gen["status"]:
                seen_status.append(gen["status"])
            if gen["status"] in TERMINAL:
                break
            await asyncio.sleep(0.25)
        print(f"      status timeline (REST polling): {' -> '.join(seen_status)} in {time.time() - t0:.1f}s")
        check("generation COMPLETED", gen.get("status") == "completed", gen)
        check("credits settled exactly once", gen.get("credit_state") == "settled", gen.get("credit_state"))
        check("provider metadata stored", (gen.get("provider_metadata") or {}).get("model") == "mock-image-v1",
              gen.get("provider_metadata"))
        assets = gen.get("assets", [])
        check(f"{a.images} assets stored", len(assets) == a.images, len(assets))

        # 5. Assets: signed URLs from the private bucket, checksum, thumbnail, isolation.
        async with httpx.AsyncClient(timeout=30) as raw:
            for asset in assets:
                d = await http.get(f"/api/v1/assets/{asset['id']}/download", headers=user)
                loc = d.headers.get("location", "")
                check(f"asset {asset['image_index']}: download -> 307 signed URL",
                      d.status_code == 307 and "X-Amz-Signature" in loc, d.status_code)
                blob = (await raw.get(loc)).content
                check(f"asset {asset['image_index']}: sha256 matches stored metadata",
                      hashlib.sha256(blob).hexdigest() == asset["sha256"] and blob[:8] == b"\x89PNG\r\n\x1a\n",
                      asset["sha256"])
                check(f"asset {asset['image_index']}: {asset['width']}x{asset['height']} {asset['mime_type']}",
                      (asset["width"], asset["height"], asset["mime_type"]) == (768, 432, "image/png"))
                th = await raw.get(asset["thumbnail_url"])
                check(f"asset {asset['image_index']}: WebP thumbnail", th.status_code == 200 and
                      th.content[8:12] == b"WEBP", th.status_code)
                check(f"asset {asset['image_index']}: bucket is private (unsigned URL denied)",
                      (await raw.get(loc.split("?")[0])).status_code == 403)

        other = await http.post("/api/v1/auth/register", json={"email": f"o-{uuid.uuid4().hex[:8]}@example.com",
                                                               "password": "other-password-123"})
        lo = await http.post("/api/v1/auth/login", json={"email": other.json()["data"]["email"],
                                                         "password": "other-password-123"})
        oh = {"Authorization": f"Bearer {lo.json()['data']['access_token']}"}
        foreign = [(await http.get(f"/api/v1/generations/{gid}", headers=oh)).status_code]
        foreign += [(await http.get(f"/api/v1/assets/{x['id']}", headers=oh)).status_code for x in assets]
        check("tenant isolation (other user gets 404)", all(c == 404 for c in foreign), foreign)

        after = (await http.get("/api/v1/user/credits", headers=user)).json()["data"]
        check("wallet debited by number_of_images",
              after["balance"] == before["balance"] - a.images and after["reserved_balance"] == 0, after)

        # 6. WebSocket notifications.
        await asyncio.sleep(1)
        reader_task.cancel()
        await ws.close()
        names = [e["event"] for e in events if e.get("data", {}).get("generation_id") == gid]
        print(f"      websocket events: {names}")
        for ev in ("generation.queued", "generation.started", "generation.progress", "generation.completed"):
            check(f"websocket delivered {ev}", ev in names, names)
        done = next((e for e in events if e["event"] == "generation.completed"), {})
        check("completed event carries event_id/timestamp/asset_ids",
              {"event_id", "event", "timestamp", "data"} <= set(done)
              and len(done.get("data", {}).get("asset_ids", [])) == a.images, done)
        check("websocket delivered credits.updated", any(e["event"] == "credits.updated" for e in events))

    print("\nE2E RESULT:", "PASS" if check.ok else "FAIL",
          f"({sum(r[1] for r in check.results)}/{len(check.results)} checks)")
    return 0 if check.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
