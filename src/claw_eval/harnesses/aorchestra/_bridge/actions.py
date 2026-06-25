"""ClawEvalAction factories — wrap task.yaml tools as AOrchestra BaseAction.

Two factories, one canonical step_log schema. Each invocation:
  1. generates a uuid4 toolCallId
  2. POSTs to the target URL with the LLM's kwargs as JSON body
  3. records exactly one entry to the shared step_log list
  4. returns the parsed response (or {"error": ...}) to the AOrchestra runtime

The step_log list is shared with ClawEvalEnv; the trace adapter (Wave 4-C)
reads it as the source of truth for ToolDispatch events.

Sandbox routing (Bash / Read / Write / etc.) requires sandbox_url. If a task
declares a SANDBOX_TOOL_NAME but no container has been started, the factory
refuses to build the action — preflight should have caught this earlier.

Phase 4 Wave 4-B Task 3 — see docs/superpowers/plans/2026-06-24-aorchestra-harness.md.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

# AOrchestra is not pip-installable. Inject its root on sys.path before the
# first BaseAction import. See docs/superpowers/specs/aorchestra_decision.md §1.
_AORCHESTRA_ROOT = os.environ.get(
    "AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra"
)
if _AORCHESTRA_ROOT not in sys.path:
    sys.path.insert(0, _AORCHESTRA_ROOT)

from base.agent.base_action import BaseAction  # noqa: E402

from ....models.tool import ToolEndpoint, ToolSpec
from ....runner.sandbox_dispatcher import SandboxToolDispatcher
from ....runner.sandbox_tools import SANDBOX_TOOL_NAMES


class SchemaTranslationError(Exception):
    """Raised when a tool spec cannot be mapped to a runnable action."""


# Reuse the dispatcher's canonical endpoint map so SANDBOX_TOOLS stays the
# single source of truth.
SANDBOX_ENDPOINTS: dict[str, str] = dict(SandboxToolDispatcher._PATH_MAP)


def _step_log_record(
    *,
    tool_call_id: str,
    tool: str,
    url: str,
    method: str,
    request: Any,
    status: int,
    response: Any,
    duration_ms: int,
    error: str | None,
    agent_role: str = "agent",
) -> dict[str, Any]:
    """Build the canonical step_log entry. Shared with env.py."""
    return {
        "toolCallId": tool_call_id,
        "agent_role": agent_role,
        "tool": tool,
        "url": url,
        "method": method,
        "request": request,
        "status": status,
        "response": response,
        "durationMs": duration_ms,
        "error": error,
    }


def _parse_response(text: str) -> Any:
    """Parse JSON when possible; fall back to raw text.

    Transport errors don't reach here (the caller wraps them with
    status=-1 + a synthetic {"error": ...} body).
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def _post(url: str, method: str, request: Any) -> tuple[int, str, str | None]:
    """POST request, returning (status, body_text, error_text_or_None).

    Transport errors yield (-1, "", str(exc)). We deliberately do not raise:
    the action contract is "always return a response object to the LLM so
    it can decide whether to retry" — same convention as OpenClaw bridge.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=method,
                url=url,
                json=request,
                headers={"content-type": "application/json"},
            )
            return resp.status_code, resp.text, None
    except Exception as exc:  # noqa: BLE001 — bridge contract: never raise
        return -1, "", str(exc)


def make_http_action(
    tool_spec: ToolSpec,
    endpoint: ToolEndpoint,
    step_log: list[dict],
    *,
    agent_role: str = "agent",
) -> BaseAction:
    """Wrap a HTTP mock service tool as a BaseAction subclass.

    ``step_log`` is a mutable list owned by ClawEvalEnv; every call appends
    one record. ``agent_role`` is set by the env when it knows whether the
    LLM call originated in MainAgent (``"main"``) or a SubAgent (``"sub"``).
    """
    _url = endpoint.url
    _method = (endpoint.method or "POST").upper()
    _tool_name = tool_spec.name
    _description = tool_spec.description
    _parameters = tool_spec.input_schema or {"type": "object", "properties": {}}
    _agent_role = agent_role

    class _HttpAction(BaseAction):
        # Pydantic class-body defaults shadow names of identical fields, so
        # we copy from underscore-prefixed closure variables that don't
        # collide with the pydantic field names.
        name: str = _tool_name
        description: str = _description
        parameters: dict = _parameters

        async def __call__(self, **kwargs: Any) -> Any:
            call_id = uuid.uuid4().hex
            started = time.monotonic()
            status, body_text, err = await _post(_url, _method, kwargs)
            duration_ms = int((time.monotonic() - started) * 1000)
            response = (
                {"error": err} if err is not None else _parse_response(body_text)
            )
            step_log.append(_step_log_record(
                tool_call_id=call_id,
                tool=_tool_name,
                url=_url,
                method=_method,
                request=kwargs,
                status=status,
                response=response,
                duration_ms=duration_ms,
                error=err,
                agent_role=_agent_role,
            ))
            return response

    return _HttpAction()


def make_sandbox_action(
    tool_spec: ToolSpec,
    sandbox_url: str | None,
    step_log: list[dict],
    *,
    agent_role: str = "agent",
) -> BaseAction:
    """Wrap a SANDBOX_TOOL_NAME tool as a BaseAction targeting the in-container
    sandbox server.

    ``sandbox_url`` MUST be non-empty when the task contains a SANDBOX_TOOL_NAME.
    Preflight (§4.1) catches this; the factory raises ``SchemaTranslationError``
    here as a second line of defence.
    """
    if not sandbox_url:
        raise SchemaTranslationError(
            f"tool {tool_spec.name!r} requires sandbox server but sandbox_url is None"
        )
    if tool_spec.name not in SANDBOX_ENDPOINTS:
        raise SchemaTranslationError(
            f"tool {tool_spec.name!r} is in SANDBOX_TOOL_NAMES but has no endpoint "
            f"in SANDBOX_ENDPOINTS — check SandboxToolDispatcher._PATH_MAP"
        )
    target_url = sandbox_url.rstrip("/") + SANDBOX_ENDPOINTS[tool_spec.name]
    endpoint = ToolEndpoint(
        tool_name=tool_spec.name,
        url=target_url,
        method="POST",
    )
    return make_http_action(tool_spec, endpoint, step_log, agent_role=agent_role)
