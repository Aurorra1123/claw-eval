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
from .env import ClawEvalEnv

__all__ = [
    "ClawEvalEnv",
    "SANDBOX_ENDPOINTS",
    "SchemaTranslationError",
    "make_http_action",
    "make_sandbox_action",
]
