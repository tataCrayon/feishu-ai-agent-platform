"""WebPortal Client — ticket management API client.

Wraps the webportal-pilot FastAPI service for ticket creation, SQL proposal
generation, and status tracking.
"""

from app.domains.webportal.client import (
    WebPortalClient,
    WebPortalConfig,
    get_client,
    reset_client,
    set_client,
)

__all__ = [
    "WebPortalClient",
    "WebPortalConfig",
    "get_client",
    "reset_client",
    "set_client",
]
