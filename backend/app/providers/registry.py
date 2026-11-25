"""Which providers the *server* accepts generations for. Execution happens on the
agents; the server only validates that a provider is enabled before queueing."""
from __future__ import annotations

from app.core.config import get_settings
from app.providers.google_flow import GoogleFlowConnector

KNOWN_PROVIDERS = ("mock", "approved", "google_flow")


def provider_states() -> dict[str, bool]:
    s = get_settings()
    states = {name: name in s.enabled_providers for name in KNOWN_PROVIDERS}
    # google_flow requires its own explicit gates in addition to ENABLED_PROVIDERS.
    states["google_flow"] = states["google_flow"] and GoogleFlowConnector().enabled
    return states


def is_enabled(provider: str) -> bool:
    return provider_states().get(provider, False)
