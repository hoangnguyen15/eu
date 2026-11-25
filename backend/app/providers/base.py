"""Provider adapter contract. The .NET agent implements the same contract in
`windows-agent/FlowAgent/Providers`; this Python version is used by the
reference agent (tests / local simulation) and by the backend registry."""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderRequest:
    task_id: str
    prompt: str
    aspect_ratio: str
    number_of_images: int
    reference_image_urls: list[str] = field(default_factory=list)
    profile_ref: str | None = None


class ProviderJobState(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class ProviderJobStatus:
    job_id: str
    state: ProviderJobState
    progress: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class ProviderImage:
    data: bytes
    mime_type: str


@dataclass
class ProviderResult:
    job_id: str
    images: list[ProviderImage]
    model: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """`code` maps to generation status on the server:
    AUTH_REQUIRED -> AUTH_REQUIRED, PROVIDER_LIMITED -> PROVIDER_LIMITED,
    REVIEW_REQUIRED / OUTCOME_UNKNOWN -> REVIEW_REQUIRED, anything else -> FAILED."""

    code = "PROVIDER_ERROR"

    def __init__(self, message: str, *, code: str | None = None, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class ProviderDisabledError(ProviderError):
    code = "PROVIDER_DISABLED"


class ProviderAuthRequiredError(ProviderError):
    code = "AUTH_REQUIRED"


class ProviderLimitedError(ProviderError):
    code = "PROVIDER_LIMITED"


class ProviderReviewRequiredError(ProviderError):
    code = "REVIEW_REQUIRED"


class ImageProvider(abc.ABC):
    name: str = "abstract"

    @property
    def enabled(self) -> bool:
        return True

    @abc.abstractmethod
    async def generate(self, request: ProviderRequest) -> str:
        """Submit a job and return the provider-side job id."""

    @abc.abstractmethod
    async def get_status(self, job_id: str) -> ProviderJobStatus:
        ...

    @abc.abstractmethod
    async def collect_result(self, job_id: str) -> ProviderResult:
        ...


ASPECT_SIZES = {
    "1:1": (512, 512),
    "16:9": (768, 432),
    "9:16": (432, 768),
    "4:3": (640, 480),
    "3:4": (480, 640),
}
