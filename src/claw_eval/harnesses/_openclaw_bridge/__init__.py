"""Bridge plugin generator package.

Phase 3 Wave 2 §6.3 — see ``docs/harness_design.md``.

Public surface:

* :class:`BridgeHandle` — per-task lifecycle handle (plugin id, traffic log,
  isolated OpenClaw state dirs).
* :func:`generate_and_install` — compile + install + enable a plugin.
* :func:`bridge_install` — context manager wrapping the above.
* :func:`compile_plugin_source` — pure source-rendering half (for tests).
* :func:`json_schema_to_typebox` — JSON Schema -> TypeBox translator.

Wave 2 deliberately stops at "source is correct, install path runs without
exceptions when ``skip_subprocess=True``". Real ``npm install`` /
``openclaw plugins`` invocations land in Wave 3 alongside ``OpenClawHarness``.
"""

from __future__ import annotations

from .generator import (
    SANDBOX_ENDPOINTS,
    BridgeHandle,
    BridgeInstallError,
    bridge_install,
    compile_plugin_source,
    derive_plugin_id,
    generate_and_install,
    parse_traffic_log,
)
from .schema_translate import SchemaTranslationError, json_schema_to_typebox

__all__ = [
    "BridgeHandle",
    "BridgeInstallError",
    "SANDBOX_ENDPOINTS",
    "SchemaTranslationError",
    "bridge_install",
    "compile_plugin_source",
    "derive_plugin_id",
    "generate_and_install",
    "json_schema_to_typebox",
    "parse_traffic_log",
]
