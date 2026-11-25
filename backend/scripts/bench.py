"""Latency / throughput benchmark against a running stack with real agents.

Creates N generations (C concurrently in flight from the client side), follows
each through the WebSocket channel and reports, per job:
  accept   = POST /generations round-trip
  dispatch = created -> generation.started   (queue wait + scheduling + accept)
  total    = created -> generation.completed (includes provider time)
plus overall throughput. Usage:
  python scripts/bench.py --api http://localhost:8000 --admin-email E --admin-password P -n 20 -c 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from typing import Any

import httpx
import websockets


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[k]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--admin-email", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("-n", type=int, default=20, help="number of generations")
    ap.add_argument("-c", type=int, default=20, help="client-side concurrency")
    ap.add_argument("--images", type=int, default=1)
    ap.add_argument("--json", action="store_true", help="print machine-readable summary")
    a = ap.parse_args()
    ws_base = a.api.replace("http", "ws", 1)

    async with httpx.AsyncClient(base_url=a.api, timeout=60) as http:
        adm = (await http.post("/api/v1/auth/login",
                               json={"email": a.admin_email, "password": a.admin_password})).json()["data"]
        admin = {"Authorization": f"Bearer {adm['access_token']}"}
        email = f"bench-{uuid.uuid4().hex[:8]}@example.com"
        reg = (await http.post("/api/v1/auth/register", json={"email": email, "password": "bench-password-1"})).json()
        await http.post("/api/v1/admin/credits/grant", json={"user_id": reg["data"]["id"], "amount": a.n * a.images},
                        headers=admin)
        tok = (await http.post("/api/v1/auth/login", json={"email": email, "password": "bench-password-1"})
               ).json()["data"]["access_token"]
        user = {"Authorization": f"Bearer {tok}"}

        created: dict[str, float] = {}
        started: dict[str, float] = {}
        finished: dict[str, tuple[float, str]] = {}
        done = asyncio.Event()
        ws = await websockets.connect(f"{ws_base}/ws/v1/events", additional_headers=user)

        async def reader() -> None:
            async for raw in ws:
                e = json.loads(raw)
                gid = e.get("data", {}).get("generation_id")
                if not gid:
                    continue
                now = time.perf_counter()
                if e["event"] == "generation.started":
                    started.setdefault(gid, now)
                elif e["event"] in ("generation.completed", "generation.failed", "generation.cancelled"):
                    finished.setdefault(gid, (now, e["data"]["status"]))
                    if len(finished) >= a.n:
                        done.set()
        rt = asyncio.create_task(reader())
        await asyncio.sleep(0.3)

        sem = asyncio.Semaphore(a.c)
        accept: list[float] = []

        async def one(i: int) -> None:
            async with sem:
                t0 = time.perf_counter()
                r = await http.post("/api/v1/generations", headers={**user, "Idempotency-Key": uuid.uuid4().hex},
                                    json={"prompt": f"bench {i}", "aspect_ratio": "1:1",
                                          "number_of_images": a.images})
                t1 = time.perf_counter()
                accept.append(t1 - t0)
                if r.status_code != 201:
                    print("create failed", r.status_code, r.text, file=sys.stderr)
                    return
                created[r.json()["data"]["generation_id"]] = t0

        wall0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(a.n)))
        try:
            await asyncio.wait_for(done.wait(), timeout=60 + a.n * 3)
        except TimeoutError:
            pass
        wall = time.perf_counter() - wall0
        rt.cancel()
        await ws.close()

    ok = [g for g, (_, s) in finished.items() if s == "completed" and g in created]
    total = [finished[g][0] - created[g] for g in ok]
    dispatch = [started[g] - created[g] for g in ok if g in started]
    summary: dict[str, Any] = {
        "jobs": a.n, "completed": len(ok), "failed_or_missing": a.n - len(ok),
        "wall_seconds": round(wall, 2), "throughput_jobs_per_s": round(len(ok) / wall, 2) if wall else 0,
        "accept_ms_p50": round(pct(accept, 50) * 1000, 1), "accept_ms_p95": round(pct(accept, 95) * 1000, 1),
        "to_started_s_p50": round(pct(dispatch, 50), 3), "to_started_s_p95": round(pct(dispatch, 95), 3),
        "total_s_p50": round(pct(total, 50), 3), "total_s_p95": round(pct(total, 95), 3),
        "total_s_mean": round(statistics.mean(total), 3) if total else None,
    }
    if a.json:
        print(json.dumps(summary))
    else:
        for k, v in summary.items():
            print(f"{k:>24}: {v}")
    return 0 if summary["failed_or_missing"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
