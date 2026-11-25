"""MockImageProvider: produces real, decodable PNG images locally so the whole
pipeline (dispatch -> upload -> verification -> storage -> notification) can be
exercised without any external service.

Control tokens in the prompt trigger failure paths for testing:
  [mock:fail]  -> PROVIDER_ERROR      [mock:auth]   -> AUTH_REQUIRED
  [mock:limit] -> PROVIDER_LIMITED    [mock:review] -> REVIEW_REQUIRED
  [mock:slow]  -> 5s extra latency
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass

from PIL import Image, ImageDraw

from app.providers.base import (
    ASPECT_SIZES,
    ImageProvider,
    ProviderAuthRequiredError,
    ProviderError,
    ProviderImage,
    ProviderJobState,
    ProviderJobStatus,
    ProviderLimitedError,
    ProviderRequest,
    ProviderResult,
    ProviderReviewRequiredError,
)


@dataclass
class _Job:
    request: ProviderRequest
    task: asyncio.Task[list[bytes]]


class MockImageProvider(ImageProvider):
    name = "mock"
    model = "mock-image-v1"

    def __init__(self, latency_seconds: float = 0.3) -> None:
        self.latency_seconds = latency_seconds
        self._jobs: dict[str, _Job] = {}

    async def generate(self, request: ProviderRequest) -> str:
        p = request.prompt
        if "[mock:auth]" in p:
            raise ProviderAuthRequiredError("Mock session requires re-authentication")
        if "[mock:limit]" in p:
            raise ProviderLimitedError("Mock provider quota reached", retry_after_seconds=60)
        if "[mock:review]" in p:
            raise ProviderReviewRequiredError("Mock provider flagged the prompt for review")
        job_id = f"mock-{uuid.uuid4().hex}"
        self._jobs[job_id] = _Job(request, asyncio.create_task(self._render(request)))
        return job_id

    async def _render(self, request: ProviderRequest) -> list[bytes]:
        delay = self.latency_seconds + (5.0 if "[mock:slow]" in request.prompt else 0.0)
        await asyncio.sleep(delay)
        if "[mock:fail]" in request.prompt:
            raise ProviderError("Mock provider failure", code="PROVIDER_ERROR")
        return [render_png(request.prompt, request.aspect_ratio, i) for i in range(request.number_of_images)]

    async def get_status(self, job_id: str) -> ProviderJobStatus:
        job = self._jobs.get(job_id)
        if job is None:
            return ProviderJobStatus(job_id, ProviderJobState.FAILED, error_code="UNKNOWN_JOB")
        if not job.task.done():
            return ProviderJobStatus(job_id, ProviderJobState.RUNNING, progress=50)
        exc = job.task.exception()
        if exc is not None:
            code = getattr(exc, "code", "PROVIDER_ERROR")
            return ProviderJobStatus(job_id, ProviderJobState.FAILED, error_code=code, error_message=str(exc))
        return ProviderJobStatus(job_id, ProviderJobState.SUCCEEDED, progress=100)

    async def collect_result(self, job_id: str) -> ProviderResult:
        job = self._jobs.pop(job_id)
        images = await job.task
        return ProviderResult(
            job_id=job_id,
            images=[ProviderImage(data=b, mime_type="image/png") for b in images],
            model=self.model,
            metadata={"seed": int(hashlib.sha256(job.request.prompt.encode()).hexdigest()[:8], 16)},
        )


def render_png(prompt: str, aspect_ratio: str, index: int) -> bytes:
    width, height = ASPECT_SIZES.get(aspect_ratio, (512, 512))
    digest = hashlib.sha256(f"{prompt}|{index}".encode()).digest()
    c1 = tuple(digest[0:3])
    c2 = tuple(digest[3:6])
    img = Image.new("RGB", (width, height), c1)
    draw = ImageDraw.Draw(img)
    for y in range(0, height, 4):
        t = y / max(1, height - 1)
        color = tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3))
        draw.rectangle([0, y, width, y + 3], fill=color)
    draw.text((16, 16), f"MOCK #{index + 1}", fill=(255, 255, 255))
    draw.text((16, 36), prompt[:60], fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
