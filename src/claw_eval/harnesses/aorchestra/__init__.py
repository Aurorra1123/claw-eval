"""AOrchestra harness package (Phase 4).

See docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md.

The AOrchestra source tree is NOT pip-installable. Callers that want to
import AOrchestra must inject its root on ``sys.path`` first — typically:

    import sys, os
    _root = os.environ.get("AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra")
    if _root not in sys.path:
        sys.path.insert(0, _root)

See ``docs/superpowers/specs/aorchestra_decision.md`` §1 for the rationale.
"""
from __future__ import annotations

# AOrchestraHarness is exported by ``aorchestra.harness`` in Wave 4-D (Task 7).
__all__: list[str] = []
