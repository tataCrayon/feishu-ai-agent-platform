"""Memory module — TencentDB Agent Memory integration.

Provides:
- MemoryClient: async HTTP client for MemoryCore Gateway (/capture, /recall, /session/end, /health)
- MemoryConfig: configuration dataclass
- Integration helpers for Channel SDK message pipeline
"""

from app.memory.client import (
    MemoryClient,
    MemoryConfig,
    get_client,
    reset_client,
    set_client,
)

__all__ = [
    "MemoryClient",
    "MemoryConfig",
    "get_client",
    "reset_client",
    "set_client",
]