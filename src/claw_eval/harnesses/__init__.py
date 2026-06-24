"""Harness registry — ``get_harness(name)`` returns a Harness instance.

Phase 3 §3.1 / §3.2 — see docs/harness_design.md.

Currently registered:

* ``claweval``  — reference implementation (Wave 1)
* ``openclaw``  — OpenClaw CLI agent (Wave 3-D host smoke + Wave 3-E container)
* ``codex``     — placeholder, raises NotImplementedError (Wave 3-F / §6.8)
* ``claudecode``— placeholder, raises NotImplementedError (Wave 3-F / §6.8)

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

__all__ = ["Harness", "HarnessFeature", "HarnessResult", "get_harness"]


# Module-level singletons. Harnesses are stateless w.r.t. tasks — one instance
# per process is fine and avoids spinning up a new object on every CLI call.
_REGISTRY: dict[str, Harness] = {
    "claweval":   ClawEvalHarness(),
    "openclaw":   OpenClawHarness(),
    "codex":      CodexHarness(),
    "claudecode": ClaudeCodeHarness(),
}


def get_harness(name: str) -> Harness:
    """Return the registered harness named ``name``.

    Raises ``KeyError`` with a helpful message if ``name`` is not known. The
    CLI's ``argparse`` ``choices`` should already have rejected unknown names
    before reaching here, so this is mostly a safety net for programmatic
    callers.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown harness: {name!r}. Registered harnesses: "
            f"{sorted(_REGISTRY)}"
        ) from None
