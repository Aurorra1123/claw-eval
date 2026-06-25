"""LLMsConfig injection — swap AOrchestra's model config singleton.

AOrchestra picks model configs from ``base.engine.async_llm.LLMsConfig._default_config``,
a class attribute that lazy-loads from yaml on first ``LLMsConfig.default()``
call. We swap that attribute in for the duration of a claw-eval run so
``main_model="claude-sonnet-4-5"`` points at the claw-eval LLM endpoint.

NOTE on the absence of a "gemini-3-flash-preview" alias entry:

Spec decision 9 originally proposed a Gemini alias mapping. Wave 4-A ticket
probe found this impossible — ``LLMsConfig.get(name)`` forcibly sets
``LLMConfig.model = llm_name`` (async_llm.py:142), so an alias dict still
sends the literal "gemini-3-flash-preview" string downstream. Instead,
``aorchestra/tools/delegate.py:266`` was patched to use ``self.models[0]``
for trace summarization (see ``docs/superpowers/specs/aorchestra_decision.md``).

So this module only needs to expose one entry: ``"claude-sonnet-4-5"``.

Phase 4 Wave 4-B Task 5 — see docs/superpowers/plans/2026-06-24-aorchestra-harness.md.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator

# AOrchestra is not pip-installable. Inject its root on sys.path before the
# first LLMsConfig import. See docs/superpowers/specs/aorchestra_decision.md §1.
_AORCHESTRA_ROOT = os.environ.get(
    "AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra"
)
if _AORCHESTRA_ROOT not in sys.path:
    sys.path.insert(0, _AORCHESTRA_ROOT)

from base.engine.async_llm import LLMsConfig  # noqa: E402

from ....config import ModelConfig


def build_llms_config(cfg_model: ModelConfig) -> LLMsConfig:
    """Synthesize an LLMsConfig with a single entry pointing at the claw-eval
    endpoint:

    ``"claude-sonnet-4-5"`` — the canonical model name used by MainAgent,
    every SubAgent, and (after the delegate.py patch) the trace summarizer.

    The inner dict uses ``"api_key"`` rather than ``"key"`` because
    ``LLMsConfig.get`` looks up that exact field name. The returned
    ``LLMConfig`` then exposes it as ``.key``.
    """
    base_url = cfg_model.base_url or ""
    api_key = cfg_model.api_key or ""
    inner = {
        "claude-sonnet-4-5": {
            "api_key": api_key,
            "base_url": base_url,
            "temperature": 0,
        },
    }
    return LLMsConfig(inner)


@contextmanager
def patched_llms_config(cfg_model: ModelConfig) -> Iterator[LLMsConfig]:
    """Swap ``LLMsConfig._default_config`` for the duration of the with-block.

    Restores the previous value on exit, even on exception. ``previous`` may
    be ``None`` (the initial state before any ``default()`` call) — restoring
    None simply causes the next ``default()`` call to re-load from yaml as
    before.
    """
    previous = LLMsConfig._default_config
    LLMsConfig._default_config = build_llms_config(cfg_model)
    try:
        yield LLMsConfig._default_config
    finally:
        LLMsConfig._default_config = previous
