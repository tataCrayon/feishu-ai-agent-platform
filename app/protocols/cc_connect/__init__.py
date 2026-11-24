"""cc-connect integration module.

Provides a client for sending prompts to cc-connect agents via webhook
and relay send.
"""

from app.protocols.cc_connect.client import (
    CCConnectClient,
    CCConnectConfig,
    get_client,
    reset_client,
    set_client,
)

__all__ = [
    "CCConnectClient",
    "CCConnectConfig",
    "get_client",
    "reset_client",
    "set_client",
]