"""LLMsConfig injection — swap AOrchestra's model config singleton.

AOrchestra picks model configs from ``base.engine.async_llm.LLMsConfig._default_config``,
a class attribute that lazy-loads from yaml on first ``LLMsConfig.default()``
call. We swap that attribute in for the duration of a claw-eval run so the
MainAgent / SubAgent's `LLMsConfig.default().get(model_id)` calls land on the
claw-eval LLM endpoint (`cfg.model.base_url` + `cfg.model.api_key`).

Phase 5 (qwen / vllm support): the alias key is now ``cfg_model.model_id``
itself, not a hardcoded ``"claude-sonnet-4-5"``. ``LLMsConfig.get(name)``
forcibly sets ``LLMConfig.model = llm_name`` (async_llm.py:142), so the
downstream HTTPS POST sends ``model=<name>`` literally — we want that name
to be whatever the upstream server accepts (e.g. ``qwen3.6-27b`` for vllm,
``claude-sonnet-4-5`` for deepwisdom's newapi). Building the alias dict
keyed by ``cfg_model.model_id`` makes the round-trip self-consistent.

NOTE on trace summarization: ``aorchestra/tools/delegate.py:266`` was
patched (commit b2750e5) to use ``self.models[0]`` for trace summarization
instead of a hardcoded ``"gemini-3-flash-preview"``. That patch is what
makes this single-entry alias dict work for every code path in AOrchestra.

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
    """Synthesize an LLMsConfig with one entry whose name is ``cfg_model.model_id``.

    AOrchestra MainAgent / SubAgent both call ``LLMsConfig.default().get(model_id)``
    using the same ``model_id`` claw-eval injected. That call forces
    ``LLMConfig.model = model_id`` (async_llm.py:142), so the upstream HTTPS
    request carries ``"model": <model_id>``. For this to work with vllm or
    any served endpoint, the alias key MUST equal the upstream-recognised
    served name — which is exactly ``cfg_model.model_id``.

    The inner dict uses ``"api_key"`` rather than ``"key"`` because
    ``LLMsConfig.get`` looks up that exact field name. The returned
    ``LLMConfig`` then exposes it as ``.key``.
    """
    base_url = cfg_model.base_url or ""
    # vllm and some private endpoints don't require an api_key; "EMPTY" is a
    # placeholder accepted by openai-sdk so the Authorization header is present
    # but the upstream ignores it. Don't leave api_key=None — it tickles
    # downstream None-handling in some clients.
    api_key = cfg_model.api_key or "EMPTY"
    model_id = cfg_model.model_id
    inner = {
        model_id: {
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
