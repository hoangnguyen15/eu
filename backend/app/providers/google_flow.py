"""GoogleFlowConnector: interface placeholder, DISABLED by default.

Google Flow is not assumed to offer a public automation API. This connector
intentionally contains no browser automation, no CAPTCHA/MFA/anti-bot bypass,
no rate-limit evasion and no cookie/session export. It can only be wired to a
concrete implementation once (1) usage rights are confirmed in writing and
(2) Google provides/permits the integration method. Until then every call
raises ProviderDisabledError.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.providers.base import (
    ImageProvider,
    ProviderDisabledError,
    ProviderJobStatus,
    ProviderRequest,
    ProviderResult,
)


class GoogleFlowConnector(ImageProvider):
    name = "google_flow"

    @property
    def enabled(self) -> bool:
        s = get_settings()
        # Even with both flags on there is no implementation: an approved
        # integration must be supplied as a separate, reviewed change.
        return bool(s.google_flow_enabled and s.google_flow_integration_confirmed and self._implementation_available)

    _implementation_available = False

    def _disabled(self) -> ProviderDisabledError:
        return ProviderDisabledError(
            "GoogleFlowConnector is disabled: no confirmed, Google-permitted integration method is configured",
            code="PROVIDER_DISABLED",
        )

    async def generate(self, request: ProviderRequest) -> str:
        raise self._disabled()

    async def get_status(self, job_id: str) -> ProviderJobStatus:
        raise self._disabled()

    async def collect_result(self, job_id: str) -> ProviderResult:
        raise self._disabled()
