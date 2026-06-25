"""Bridge modules: HTTP action factories, env adapter, LLMsConfig patch.

Phase 4 §3.4a / §4.2-4.4.
"""
from __future__ import annotations

from .actions import (
    SANDBOX_ENDPOINTS,
    SchemaTranslationError,
    make_http_action,
    make_sandbox_action,
)

__all__ = [
    "SANDBOX_ENDPOINTS",
    "SchemaTranslationError",
    "make_http_action",
    "make_sandbox_action",
]
