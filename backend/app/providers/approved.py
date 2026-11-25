"""ApprovedImageProvider: adapter for an image-generation API you hold a licence
/ API agreement for. The HTTP contract below is a neutral template; map it to
the real vendor API (endpoints, auth header, payload) before enabling.

Expected contract (configurable base URL, bearer API key):
  POST {base}/v1/images/jobs  {prompt, aspect_ratio, n}  -> {"id": "..."}
  GET  {base}/v1/images/jobs/{id} -> {"status": "pending|running|succeeded|failed",
                                     "progress": 0-100, "images": [{"url": "..."}],
                                     "model": "...", "error": {"code","message"}}
HTTP 401/403 -> AUTH_REQUIRED, 429 -> PROVIDER_LIMITED (no automatic account switching).
"""
from __future__ import annotations

import os

import httpx

from app.providers.base import (
    ImageProvider,
    ProviderAuthRequiredError,
    ProviderDisabledError,
    ProviderError,
    ProviderImage,
    ProviderJobState,
    ProviderJobStatus,
    ProviderLimitedError,
    ProviderRequest,
    ProviderResult,
)

_STATE = {"pending": ProviderJobState.PENDING, "running": ProviderJobState.RUNNING,
          "succeeded": ProviderJobState.SUCCEEDED, "failed": ProviderJobState.FAILED}


class ApprovedImageProvider(ImageProvider):
    name = "approved"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or os.environ.get("APPROVED_PROVIDER_BASE_URL") or "").rstrip("/")
        self._api_key = api_key or os.environ.get("APPROVED_PROVIDER_API_KEY")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self._api_key)

    def _client(self) -> httpx.AsyncClient:
        if not self.enabled:
            raise ProviderDisabledError("ApprovedImageProvider is not configured")
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout,
                                 headers={"Authorization": f"Bearer {self._api_key}"})

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise ProviderAuthRequiredError(f"Provider rejected credentials ({resp.status_code})")
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After")
            raise ProviderLimitedError("Provider rate limit", retry_after_seconds=int(retry) if retry and
                                       retry.isdigit() else None)
        if resp.status_code >= 400:
            raise ProviderError(f"Provider HTTP {resp.status_code}")

    async def generate(self, request: ProviderRequest) -> str:
        async with self._client() as c:
            resp = await c.post("/v1/images/jobs", json={"prompt": request.prompt,
                                                         "aspect_ratio": request.aspect_ratio,
                                                         "n": request.number_of_images})
            self._raise_for(resp)
            return str(resp.json()["id"])

    async def get_status(self, job_id: str) -> ProviderJobStatus:
        async with self._client() as c:
            resp = await c.get(f"/v1/images/jobs/{job_id}")
            self._raise_for(resp)
            body = resp.json()
            err = body.get("error") or {}
            return ProviderJobStatus(job_id, _STATE.get(body.get("status", ""), ProviderJobState.FAILED),
                                     progress=int(body.get("progress", 0)), error_code=err.get("code"),
                                     error_message=err.get("message"))

    async def collect_result(self, job_id: str) -> ProviderResult:
        async with self._client() as c:
            resp = await c.get(f"/v1/images/jobs/{job_id}")
            self._raise_for(resp)
            body = resp.json()
            images: list[ProviderImage] = []
            for item in body.get("images", []):
                img = await c.get(item["url"])
                self._raise_for(img)
                images.append(ProviderImage(data=img.content,
                                            mime_type=img.headers.get("content-type", "image/png").split(";")[0]))
            return ProviderResult(job_id=job_id, images=images, model=body.get("model"),
                                  metadata={"provider_job_id": job_id})
