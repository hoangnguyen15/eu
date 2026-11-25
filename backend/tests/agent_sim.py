"""Python reference agent speaking the real worker protocol over WebSocket.

It talks to the backend's /ws/v1/agent endpoint the way the mTLS gateway
would (forwarding the verified client certificate + shared secret), and
executes tasks with the Python MockImageProvider. Used by integration tests;
the production agent is the .NET service in windows-agent/.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
import websockets

from app.providers.base import ProviderError, ProviderJobState, ProviderRequest
from app.providers.mock import MockImageProvider


@dataclass
class SimAgent:
    ws_url: str
    gateway_secret: str
    cert_pem: str
    capacity: int = 1
    providers: list[str] = field(default_factory=lambda: ["mock"])
    sessions: list[dict[str, str]] = field(default_factory=list)
    auto_accept: bool = True
    execute: bool = True
    corrupt_upload: bool = False
    heartbeat_interval: float = 1.0
    provider: MockImageProvider = field(default_factory=lambda: MockImageProvider(latency_seconds=0.2))

    def __post_init__(self) -> None:
        self.offers: list[dict[str, Any]] = []
        self.acks: dict[str, dict[str, Any]] = {}
        self.cancels: list[dict[str, Any]] = []
        self.session_checks = 0
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.received: list[dict[str, Any]] = []
        self._ack_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ws: Any = None
        self._tasks: list[asyncio.Task[Any]] = []
        self.registered = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_code: int | None = None

    # ------------------------------------------------------------ plumbing --
    def headers(self) -> dict[str, str]:
        return {"X-Gateway-Secret": self.gateway_secret, "X-Client-Verify": "SUCCESS",
                "X-Client-Cert": quote(self.cert_pem)}

    async def start(self) -> None:
        self._ws = await websockets.connect(self.ws_url, additional_headers=self.headers(), max_size=2**22)
        self._tasks.append(asyncio.create_task(self._reader()))
        await self.send_event("REGISTER", {"hostname": "sim-host", "agent_version": "sim-1.0",
                                           "capacity": self.capacity, "providers": self.providers},
                              persisted=False)
        await asyncio.wait_for(self.registered.wait(), 5)
        if self.sessions:
            await self.send_event("SESSION_STATUS", {"sessions": self.sessions})
        self._tasks.append(asyncio.create_task(self._heartbeat()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._tasks.clear()

    async def kill(self) -> None:
        """Simulate a crash: stop heartbeats and drop the socket abruptly."""
        await self.stop()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            with contextlib.suppress(Exception):
                await self._raw_send({"type": "HEARTBEAT", "id": uuid.uuid4().hex,
                                      "payload": {"event_id": uuid.uuid4().hex, "active_tasks": []}})

    async def _raw_send(self, msg: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(msg))

    async def send_event(self, msg_type: str, payload: dict[str, Any], *, persisted: bool = True,
                         event_id: str | None = None, wait: bool = True) -> dict[str, Any]:
        event_id = event_id or uuid.uuid4().hex
        payload = {**payload, "event_id": event_id}
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._ack_waiters[event_id] = fut
        await self._raw_send({"type": msg_type, "id": uuid.uuid4().hex, "payload": payload})
        if not wait:
            return {}
        return await asyncio.wait_for(fut, 15)

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                self.received.append(msg)
                t = msg.get("type")
                pl = msg.get("payload", {})
                if t == "EVENT_ACK":
                    eid = pl.get("event_id")
                    self.acks[eid] = pl
                    if "workstation_id" in pl.get("result", {}):
                        self.registered.set()
                    fut = self._ack_waiters.pop(eid, None)
                    if fut and not fut.done():
                        fut.set_result(pl)
                elif t == "TASK_OFFER":
                    self.offers.append(pl)
                    if self.auto_accept:
                        asyncio.create_task(self._run_task(pl))
                elif t == "TASK_CANCEL":
                    self.cancels.append(pl)
                elif t == "SESSION_CHECK":
                    self.session_checks += 1
                    asyncio.create_task(self.send_event("SESSION_STATUS", {"sessions": self.sessions}, wait=False))
        except websockets.ConnectionClosed as exc:
            self.close_code = exc.rcvd.code if exc.rcvd else None
        finally:
            self.closed.set()

    # ------------------------------------------------------------ task flow --
    async def _run_task(self, offer: dict[str, Any]) -> None:
        task_id = offer["task_id"]
        ack = await self.send_event("TASK_ACCEPT", {"task_id": task_id})
        if ack["status"] != "ok" or not self.execute:
            return
        await self.send_event("TASK_STARTED", {"task_id": task_id})
        req = ProviderRequest(task_id=task_id, prompt=offer["prompt"], aspect_ratio=offer["aspect_ratio"],
                              number_of_images=offer["number_of_images"], profile_ref=offer.get("profile_ref"))
        try:
            job_id = await self.provider.generate(req)
            while True:
                st = await self.provider.get_status(job_id)
                if st.state in (ProviderJobState.SUCCEEDED, ProviderJobState.FAILED):
                    break
                await self.send_event("TASK_PROGRESS", {"task_id": task_id, "progress": 50})
                await asyncio.sleep(0.1)
            if st.state == ProviderJobState.FAILED:
                raise ProviderError(st.error_message or "failed", code=st.error_code)
            result = await self.provider.collect_result(job_id)
        except ProviderError as exc:
            await self.send_event("TASK_FAILED", {"task_id": task_id, "error_code": exc.code,
                                                  "error_message": exc.message,
                                                  "retry_after_seconds": exc.retry_after_seconds})
            self.failed.append(task_id)
            return

        files = [{"index": i, "mime_type": img.mime_type, "size": len(img.data),
                  "sha256": hashlib.sha256(img.data).hexdigest()} for i, img in enumerate(result.images)]
        up = await self.send_event("UPLOAD_REQUEST", {"task_id": task_id, "files": files})
        if up["status"] != "ok":
            self.failed.append(task_id)
            return
        async with httpx.AsyncClient(timeout=30) as http:
            for u in up["result"]["uploads"]:
                body = result.images[u["index"]].data
                if self.corrupt_upload:
                    body = body[:-10] + b"0123456789"
                r = await http.put(u["url"], content=body, headers=u["headers"])
                r.raise_for_status()
        done = await self.send_event("UPLOAD_COMPLETED", {"task_id": task_id, "files": [
            {**f, "storage_key": u["storage_key"]} for f, u in zip(files, up["result"]["uploads"], strict=True)]})
        if done["status"] != "ok":
            self.failed.append(task_id)
            return
        fin = await self.send_event("TASK_COMPLETED", {"task_id": task_id, "provider": "mock",
                                                       "model": result.model,
                                                       "provider_metadata": result.metadata})
        if fin["status"] == "ok":
            self.completed.append(task_id)
