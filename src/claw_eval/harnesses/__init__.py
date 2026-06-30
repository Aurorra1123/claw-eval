"""Harness registry — ``get_harness(name)`` returns a Harness instance.

Phase 3 §3.1 / §3.2 — see docs/harness_design.md.

Currently registered:

* ``claweval``   — reference implementation (Wave 1)
* ``openclaw``   — OpenClaw CLI agent (Wave 3-D host smoke + Wave 3-E container)
* ``aorchestra`` — AOrchestra MainAgent + SubAgent (Phase 4 Wave 4-D host smoke)
* ``codex``      — placeholder, raises NotImplementedError (Wave 3-F / §6.8)
* ``claudecode`` — placeholder, raises NotImplementedError (Wave 3-F / §6.8)

Codex and Claude Code occupy registry slots so the CLI's ``--harness``
choices accept them as valid names — running a task through either of them
fails loudly at ``preflight`` / ``run``, not silently with a bogus trace.
"""

from __future__ import annotations

from .base import Harness, HarnessFeature, HarnessResult
from .claudecode import ClaudeCodeHarness
from .claweval import ClawEvalHarness
from .codex import CodexHarness
from .openclaw import OpenClawHarness

# AOrchestra depends on the external AOrchestra framework (the ``base`` package),
# which is deliberately NOT pip-installable — its source root must be injected
# via PYTHONPATH / ``AORCHESTRA_ROOT`` (see the ``aorchestra`` extra in
# pyproject). When that source is absent, importing the AO harness raises
# ImportError. The AO arm is OPTIONAL: a missing AOrchestra source must not take
# down the whole registry (claweval / openclaw / placeholders don't depend on
# it), so the import is best-effort. If it fails, the ``aorchestra`` slot stays
# unregistered and ``get_harness("aorchestra")`` fails loudly at call time with a
# pointer to the missing dependency — never at import time.
try:
    from .aorchestra import AOrchestraHarness

    _AORCHESTRA_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # ImportError, plus any transitive failure from ``base``
    AOrchestraHarness = None  # type: ignore[assignment,misc]
    _AORCHESTRA_IMPORT_ERROR = exc

__all__ = ["Harness", "HarnessFeature", "HarnessResult", "get_harness"]


# Module-level singletons. Harnesses are stateless w.r.t. tasks — one instance
# per process is fine and avoids spinning up a new object on every CLI call.
_REGISTRY: dict[str, Harness] = {
    "claweval":   ClawEvalHarness(),
    "claudecode": ClaudeCodeHarness(),
    "codex":      CodexHarness(),
    "openclaw":   OpenClawHarness(),
}
if AOrchestraHarness is not None:
    _REGISTRY["aorchestra"] = AOrchestraHarness()


def get_harness(name: str) -> Harness:
    """Return the registered harness named ``name``.

    Raises ``KeyError`` with a helpful message if ``name`` is not known. The
    CLI's ``argparse`` ``choices`` should already have rejected unknown names
    before reaching here, so this is mostly a safety net for programmatic
    callers.

    The ``aorchestra`` harness is optional (see the import note above): if its
    external AOrchestra source is unavailable this raises a clear error pointing
    at ``AORCHESTRA_ROOT`` instead of a bare KeyError.
    """
    if name == "aorchestra" and "aorchestra" not in _REGISTRY:
        raise RuntimeError(
            "The 'aorchestra' harness requires the external AOrchestra framework "
            "(the 'base' package), which is not pip-installable. Inject its source "
            "via AORCHESTRA_ROOT / PYTHONPATH (see the 'aorchestra' extra in "
            f"pyproject.toml). Original import error: {_AORCHESTRA_IMPORT_ERROR!r}"
        )
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown harness: {name!r}. Registered harnesses: "
            f"{sorted(_REGISTRY)}"
        ) from None
