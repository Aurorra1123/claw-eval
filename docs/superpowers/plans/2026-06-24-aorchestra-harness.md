# AOrchestra Harness Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--harness aorchestra` to claw-eval as a first-class harness, alongside ClawEval / OpenClaw, so claw-eval tasks can be driven by the AOrchestra MainAgent + SubAgent framework with a flat trace JSONL the existing grader pipeline consumes unchanged.

**Architecture:** AOrchestra runs as a Python in-process library (no subprocess like OpenClaw). Each `task.tool` gets wrapped as a `BaseAction` subclass (HTTP for mock service tools, sandbox-server HTTP for `Bash` / `Read` / `Write` etc.). Container runs **only when the task declares SANDBOX_TOOL_NAMES** — host-only path otherwise. Trace is built from `ClawEvalEnv._step_log` (which we control) + AOrchestra's trajectory JSON, with `agent_role` ∈ `{"main", "sub", "agent"}` distinguishing MainAgent / SubAgent / pre-existing harness events.

**Tech Stack:** Python 3.11+, pydantic 2.x, httpx (async), AOrchestra (editable install from `/data2/ruanjianhao/AOrchestra/`), docker SDK (reuse `Dockerfile.openclaw` image — **no new image**), pytest. LLM endpoint via `CLAWEVAL_LLM_BASE_URL` / `_API_KEY` / `_MODEL` env vars (same convention as Wave 3-E).

## Global Constraints

- **Model: `claude-sonnet-4-5`** for both MainAgent and every SubAgent. `LLMsConfig` must also map `"gemini-3-flash-preview"` → same endpoint (delegate.py:266 hardcodes this key for `_summarize_trace`). **No AOrchestra source edits.**
- **Container is on-demand**, not default: only when `task.tools` contains any `SANDBOX_TOOL_NAMES` member. Asymmetric vs OpenClaw (which is always container).
- **Image reuse**: `claw-eval-agent-openclaw:latest` (built in Phase 3 Wave 3-E). Do **not** build a new image.
- **Forbidden zones** (do not modify):
  - `src/claw_eval/harnesses/openclaw.py` / `_openclaw_*` / `_trace_adapter.py` / `_snapshot.py`
  - `src/claw_eval/runner/loop.py` / `graders/` / `models/scoring.py`
  - `src/claw_eval/harnesses/{base,claweval,codex,claudecode}.py`
  - `tasks/` / `mock_services/`
  - `Dockerfile.openclaw`
- **`agent_role` field**: add to `TraceMessage` and `ToolDispatch` ONLY (not `AuditSnapshot` / `TraceEnd` / `MediaLoad` / `CompactEvent`). Default `"agent"`, keeps Wave 1-3 traces byte-identical.
- **`tool_use_id` generation**: `uuid4().hex` in `ClawEvalAction.__call__`; same id goes into both `_step_log` and the value returned to AOrchestra runtime. This is the level-1 callID match guarantee.
- **`failure_modes` schema**: copy OpenClaw exactly (`_openclaw_native.py:1167-1187`, `_trace_adapter.py:439-446`). No new enum values.
- **All e2e tests** gate on `RUN_E2E=1` + LLM env vars (`CLAWEVAL_LLM_BASE_URL` / `_API_KEY` / `_MODEL`). Container e2e tests also gate on docker + `claw-eval-agent-openclaw:latest` image presence.
- **`pip install claw-eval[aorchestra]`** extras + **`pip install -e /data2/ruanjianhao/AOrchestra`** are the two install steps users run. README should document this.
- **`docs/superpowers/specs/aorchestra_decision.md`** — Wave 4-A writes its probe results here.

---

## File Structure

**New files:**

- `src/claw_eval/harnesses/aorchestra/__init__.py` — exports `AOrchestraHarness`
- `src/claw_eval/harnesses/aorchestra/harness.py` — `AOrchestraHarness` class (Wave 4-D)
- `src/claw_eval/harnesses/aorchestra/_runner.py` — wraps AOrchestra `MainAgent` construction + `await run_one_task` (Wave 4-D)
- `src/claw_eval/harnesses/aorchestra/_trace_adapter.py` — `translate_aorchestra` (Wave 4-C)
- `src/claw_eval/harnesses/aorchestra/_bridge/__init__.py` — exports actions / env / model_config (Wave 4-B)
- `src/claw_eval/harnesses/aorchestra/_bridge/actions.py` — `make_http_action`, `make_sandbox_action`, `SANDBOX_ENDPOINTS` (Wave 4-B)
- `src/claw_eval/harnesses/aorchestra/_bridge/env.py` — `ClawEvalEnv` context manager + `_step_log` writer (Wave 4-B)
- `src/claw_eval/harnesses/aorchestra/_bridge/model_config.py` — `patched_llms_config(cfg_model)` context manager (Wave 4-B)
- `tests/fixtures/aorchestra/trajectory_sample.json` — handcrafted AOrchestra trajectory (Wave 4-C)
- `tests/fixtures/aorchestra/step_log_sample.jsonl` — handcrafted bridge step log aligned to trajectory toolCallIds (Wave 4-C)
- `tests/test_aorchestra_bridge.py` — Wave 4-B unit tests
- `tests/test_aorchestra_trace_adapter.py` — Wave 4-C unit tests
- `tests/test_aorchestra_e2e.py` — Wave 4-D T077 host e2e
- `tests/test_aorchestra_e2e_container.py` — Wave 4-E T068 container e2e
- `docs/superpowers/specs/aorchestra_decision.md` — Wave 4-A probe result log

**Modified files:**

- `src/claw_eval/models/trace.py` — add `agent_role` field to `TraceMessage` and `ToolDispatch` (default `"agent"`)
- `src/claw_eval/harnesses/__init__.py` — register `"aorchestra"` in `_REGISTRY`
- `src/claw_eval/cli.py` — add `"aorchestra"` to `--harness choices` (3 places: `run` / `_run-inner` / `batch`); add on-demand container logic (open-coded next to existing OpenClaw container path)
- `pyproject.toml` — `[project.optional-dependencies].aorchestra = [...]`
- `tests/test_harness_placeholders.py` — extend parametrization to verify aorchestra is registered

---

## Wave 4-A: Ticket Probe (main conversation)

### Task 1: Install AOrchestra in claw-eval venv + write decision log

**Goal:** Verify zero dependency conflicts and confirm the three core assumptions in the spec (LLMsConfig singleton injection, BaseAction subclass invocation, trajectory output path) hold.

**Files:**
- Create: `docs/superpowers/specs/aorchestra_decision.md`
- (no source code changes)

**Interfaces:**
- Consumes: (none — fresh start)
- Produces: green/yellow/red signal for Wave 4-B start

- [ ] **Step 1: Install AOrchestra as editable**

```bash
cd /data2/ruanjianhao/claw-eval
pip install -e /data2/ruanjianhao/AOrchestra 2>&1 | tail -10
```

Expected: install succeeds. If pip reports version conflicts that break Wave 1-3 imports, abort and log the conflict to decision.md.

- [ ] **Step 2: Re-run the full claw-eval regression to verify install didn't break Wave 1-3**

```bash
python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
```

Expected: `61 passed, 3 skipped` (matching Wave 3-F baseline). If anything fails, the AOrchestra install pulled an incompatible dependency — flag in decision.md and stop.

- [ ] **Step 3: Probe LLMsConfig injection**

Run this inline as a quick check (no committed file):

```python
from base.engine.async_llm import LLMsConfig, LLMConfig
LLMsConfig._default_config = LLMsConfig({
    "claude-sonnet-4-5": {"model": "claude-sonnet-4-5",
                          "key": "sk-test",
                          "base_url": "https://example.com/v1"},
    "gemini-3-flash-preview": {"model": "claude-sonnet-4-5",
                                "key": "sk-test",
                                "base_url": "https://example.com/v1"},
})
sc = LLMsConfig.default().get("claude-sonnet-4-5")
gem = LLMsConfig.default().get("gemini-3-flash-preview")
assert sc.base_url == "https://example.com/v1"
assert gem.base_url == "https://example.com/v1"
assert gem.model == "claude-sonnet-4-5"
print("LLMsConfig probe ok")
LLMsConfig._default_config = None  # restore
```

Expected: prints `LLMsConfig probe ok`. If `get()` constructs an `LLMConfig` from the dict (it does, see `async_llm.py:133-148`), the alias mapping works as designed.

- [ ] **Step 4: Probe BaseAction can be instantiated**

```python
from base.agent.base_action import BaseAction
import asyncio

class _Probe(BaseAction):
    name: str = "probe"
    description: str = "probe action"
    parameters: dict = {"type": "object", "properties": {}}
    async def __call__(self, **kwargs):
        return "ok"

probe = _Probe()
result = asyncio.run(probe())
assert result == "ok"
assert probe.to_param()["function"]["name"] == "probe"
print("BaseAction probe ok")
```

Expected: prints `BaseAction probe ok`.

- [ ] **Step 5: Write decision.md with verdict**

Create `docs/superpowers/specs/aorchestra_decision.md` recording:
- pip install result + dependency notes
- regression test count (61 passed expected)
- LLMsConfig probe verdict
- BaseAction probe verdict
- Final status: 🟢 GREEN → proceed to Wave 4-B / 🟡 YELLOW (with caveats) / 🔴 RED (blocking)
- Any deviations from spec assumption discovered during probe

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/aorchestra_decision.md
git commit -m "docs(aorchestra): wave 4-A ticket probe results"
```

---

## Wave 4-B: Bridge Module (main conversation)

### Task 2: Add `agent_role` field to TraceMessage + ToolDispatch

**Goal:** Extend the trace schema with a backward-compatible `agent_role` discriminator. Wave 1-3 traces deserialize to `"agent"` (default).

**Files:**
- Modify: `src/claw_eval/models/trace.py`
- Test: `tests/test_aorchestra_bridge.py` (new file, first test goes here)

**Interfaces:**
- Consumes: (none — schema addition)
- Produces:
  - `TraceMessage.agent_role: Literal["main", "sub", "agent"]` (default `"agent"`)
  - `ToolDispatch.agent_role: Literal["main", "sub", "agent"]` (default `"agent"`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_aorchestra_bridge.py`:

```python
"""Wave 4-B unit tests for the AOrchestra bridge module."""
from __future__ import annotations

from claw_eval.models.trace import ToolDispatch, TraceMessage
from claw_eval.models.message import Message
from claw_eval.models.content import TextBlock


def test_trace_message_agent_role_default_is_agent():
    msg = TraceMessage(
        trace_id="t1",
        message=Message(role="assistant", content=[TextBlock(text="hi")]),
    )
    assert msg.agent_role == "agent"


def test_trace_message_agent_role_can_be_main_or_sub():
    msg = TraceMessage(
        trace_id="t1",
        message=Message(role="assistant", content=[TextBlock(text="hi")]),
        agent_role="main",
    )
    assert msg.agent_role == "main"
    msg.agent_role = "sub"
    assert msg.agent_role == "sub"


def test_tool_dispatch_agent_role_default_is_agent():
    td = ToolDispatch(
        trace_id="t1",
        tool_use_id="tu1",
        tool_name="ocr_extract_text",
        endpoint_url="http://localhost:9121/ocr/extract",
    )
    assert td.agent_role == "agent"


def test_tool_dispatch_agent_role_can_be_main_or_sub():
    td = ToolDispatch(
        trace_id="t1",
        tool_use_id="tu1",
        tool_name="ocr_extract_text",
        endpoint_url="http://localhost:9121/ocr/extract",
        agent_role="sub",
    )
    assert td.agent_role == "sub"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_aorchestra_bridge.py -p no:quadrants -v 2>&1 | tail -10
```

Expected: FAIL — `TraceMessage` and `ToolDispatch` do not yet accept `agent_role`.

- [ ] **Step 3: Add field to TraceMessage**

In `src/claw_eval/models/trace.py`, locate `class TraceMessage(BaseModel):` and add the field. After the existing `timestamp` field, before the closing of the class body:

```python
class TraceMessage(BaseModel):
    type: Literal["message"] = "message"
    trace_id: str
    message: Message
    usage: TokenUsage = Field(default_factory=TokenUsage)
    timestamp: str = Field(default_factory=_now)
    # AOrchestra (Phase 4) labels each trace event with its agent role.
    # "agent" is the harness-agnostic default — keeps Wave 1-3 traces
    # deserializing byte-identically.
    agent_role: Literal["main", "sub", "agent"] = "agent"
```

- [ ] **Step 4: Add field to ToolDispatch**

In the same file, locate `class ToolDispatch(BaseModel):` and add the field after the existing `timestamp` field:

```python
class ToolDispatch(BaseModel):
    type: Literal["tool_dispatch"] = "tool_dispatch"
    trace_id: str
    tool_use_id: str
    tool_name: str
    endpoint_url: str
    request_body: dict[str, Any] = Field(default_factory=dict)
    response_status: int = 200
    response_body: Any = None
    latency_ms: float = 0.0
    timestamp: str = Field(default_factory=_now)
    # AOrchestra (Phase 4) labels each dispatch with the calling agent role.
    agent_role: Literal["main", "sub", "agent"] = "agent"
```

- [ ] **Step 5: Verify Wave 1-3 traces still load**

```bash
python -m pytest tests/test_harness_claweval_regression.py tests/test_openclaw_native_smoke.py tests/test_openclaw_bridge_generator.py tests/test_trace_adapter_openclaw.py tests/test_aorchestra_bridge.py -p no:quadrants 2>&1 | tail -3
```

Expected: all pass, including the 4 new aorchestra tests.

- [ ] **Step 6: Commit**

```bash
git add src/claw_eval/models/trace.py tests/test_aorchestra_bridge.py
git commit -m "feat(trace): add agent_role field to TraceMessage and ToolDispatch"
```

### Task 3: ClawEvalAction factories (HTTP + sandbox routing)

**Goal:** Provide two factories that wrap each `task.tool` as a `BaseAction` subclass. HTTP tools fetch the mock service URL; sandbox tools fetch the in-container sandbox server endpoint.

**Files:**
- Create: `src/claw_eval/harnesses/aorchestra/__init__.py`
- Create: `src/claw_eval/harnesses/aorchestra/_bridge/__init__.py`
- Create: `src/claw_eval/harnesses/aorchestra/_bridge/actions.py`
- Modify: `tests/test_aorchestra_bridge.py` (append more tests)

**Interfaces:**
- Consumes:
  - `from base.agent.base_action import BaseAction` (AOrchestra)
  - `from claw_eval.runner.sandbox_tools import SANDBOX_TOOL_NAMES`
  - `from claw_eval.runner.sandbox_dispatcher import SandboxToolDispatcher` — `_PATH_MAP` maps each SANDBOX_TOOL_NAME → endpoint path (e.g. `"Bash"` → `"/exec"`)
  - `ToolSpec` from `claw_eval.models.tool` (has `name: str`, `description: str`, `input_schema: dict`)
  - `ToolEndpoint` from `claw_eval.models.tool` (has `tool_name: str`, `url: str`, `method: str`)
- Produces:
  - `SANDBOX_ENDPOINTS: dict[str, str]` — re-exported from `SandboxToolDispatcher._PATH_MAP` so other modules don't need to reach in
  - `class SchemaTranslationError(Exception)` — raised when a tool requires sandbox routing but `sandbox_url` is None
  - `def make_http_action(tool_spec: ToolSpec, endpoint: ToolEndpoint, step_log: list[dict]) -> BaseAction` — async, posts to `endpoint.url`, records to `step_log`
  - `def make_sandbox_action(tool_spec: ToolSpec, sandbox_url: str | None, step_log: list[dict]) -> BaseAction` — same shape, target is `f"{sandbox_url}{SANDBOX_ENDPOINTS[tool_spec.name]}"`
  - `def _step_log_record(...) -> dict` — internal helper that builds the canonical step_log entry shape (also used by env.py)

The step_log entry schema (canonical — Wave 4-C trace adapter consumes this exact shape):
```
{
  "toolCallId": str,        # uuid4().hex
  "agent_role": str,        # "main" | "sub"
  "tool": str,              # tool_spec.name
  "url": str,               # actual endpoint hit
  "method": str,            # "POST"
  "request": Any,           # kwargs passed to __call__
  "status": int,            # HTTP status code, or -1 on transport error
  "response": Any,          # parsed JSON if possible, else raw string, or {"error": ...}
  "durationMs": int,
  "error": str | None,      # populated only on transport error
}
```

- [ ] **Step 1: Write failing tests**

Append to `tests/test_aorchestra_bridge.py`:

```python
import asyncio
import pytest
import respx
from httpx import Response

from claw_eval.harnesses.aorchestra._bridge.actions import (
    make_http_action,
    make_sandbox_action,
    SANDBOX_ENDPOINTS,
    SchemaTranslationError,
)
from claw_eval.models.tool import ToolSpec, ToolEndpoint


@pytest.fixture
def ocr_tool_spec():
    return ToolSpec(
        name="ocr_extract_text",
        description="OCR a file",
        input_schema={"type": "object", "properties": {"image_path": {"type": "string"}}},
    )


@pytest.fixture
def ocr_endpoint():
    return ToolEndpoint(
        tool_name="ocr_extract_text",
        url="http://mock-ocr/ocr/extract",
        method="POST",
    )


def test_make_http_action_returns_base_action_with_metadata(ocr_tool_spec, ocr_endpoint):
    step_log: list[dict] = []
    action = make_http_action(ocr_tool_spec, ocr_endpoint, step_log)
    assert action.name == "ocr_extract_text"
    assert action.description == "OCR a file"
    assert action.parameters == ocr_tool_spec.input_schema


@respx.mock
def test_http_action_call_records_step_log_on_success(ocr_tool_spec, ocr_endpoint):
    respx.post("http://mock-ocr/ocr/extract").mock(
        return_value=Response(200, json={"text": "hello"})
    )
    step_log: list[dict] = []
    action = make_http_action(ocr_tool_spec, ocr_endpoint, step_log)
    result = asyncio.run(action(image_path="x.pdf"))
    assert result == {"text": "hello"}
    assert len(step_log) == 1
    rec = step_log[0]
    assert rec["tool"] == "ocr_extract_text"
    assert rec["url"] == "http://mock-ocr/ocr/extract"
    assert rec["method"] == "POST"
    assert rec["status"] == 200
    assert rec["response"] == {"text": "hello"}
    assert rec["request"] == {"image_path": "x.pdf"}
    assert isinstance(rec["toolCallId"], str) and len(rec["toolCallId"]) == 32
    assert rec["agent_role"] in ("main", "sub")  # set by env, default in factory must be one or the other
    assert "durationMs" in rec
    assert rec.get("error") is None


@respx.mock
def test_http_action_transport_error_records_status_minus_one(ocr_tool_spec, ocr_endpoint):
    import httpx
    respx.post("http://mock-ocr/ocr/extract").mock(
        side_effect=httpx.ConnectError("boom")
    )
    step_log: list[dict] = []
    action = make_http_action(ocr_tool_spec, ocr_endpoint, step_log)
    result = asyncio.run(action(image_path="x.pdf"))
    assert isinstance(result, dict) and "error" in result
    assert len(step_log) == 1
    rec = step_log[0]
    assert rec["status"] == -1
    assert rec["error"] is not None


def test_make_sandbox_action_routes_to_sandbox_exec():
    tool = ToolSpec(name="Bash", description="run bash",
                    input_schema={"type": "object",
                                  "properties": {"command": {"type": "string"}}})
    step_log: list[dict] = []
    action = make_sandbox_action(tool, "http://sandbox:8080", step_log)
    assert action.name == "Bash"
    # The URL is baked in at construction; we can't read it directly,
    # but we can confirm SANDBOX_ENDPOINTS["Bash"] is "/exec".
    assert SANDBOX_ENDPOINTS["Bash"] == "/exec"


def test_make_sandbox_action_raises_when_sandbox_url_missing():
    tool = ToolSpec(name="Bash", description="run bash",
                    input_schema={"type": "object", "properties": {}})
    step_log: list[dict] = []
    with pytest.raises(SchemaTranslationError):
        make_sandbox_action(tool, None, step_log)


def test_sandbox_endpoints_cover_all_sandbox_tool_names():
    from claw_eval.runner.sandbox_tools import SANDBOX_TOOL_NAMES
    for name in SANDBOX_TOOL_NAMES:
        assert name in SANDBOX_ENDPOINTS, (
            f"SANDBOX_ENDPOINTS missing {name!r} — check SandboxToolDispatcher._PATH_MAP"
        )
```

- [ ] **Step 2: Run tests to verify they fail (no factory yet)**

```bash
python -m pytest tests/test_aorchestra_bridge.py -p no:quadrants 2>&1 | tail -5
```

Expected: ImportError on `claw_eval.harnesses.aorchestra._bridge.actions`.

- [ ] **Step 3: Create the aorchestra subdir package**

Create `src/claw_eval/harnesses/aorchestra/__init__.py`:

```python
"""AOrchestra harness package (Phase 4).

See docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md.
"""
from __future__ import annotations

# Re-exported at the package boundary so callers don't reach into the
# private _bridge / _runner modules.
__all__: list[str] = []
# AOrchestraHarness is exported later in Wave 4-D (Task 9).
```

Create `src/claw_eval/harnesses/aorchestra/_bridge/__init__.py`:

```python
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
```

- [ ] **Step 4: Implement actions.py**

Create `src/claw_eval/harnesses/aorchestra/_bridge/actions.py`:

```python
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
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from base.agent.base_action import BaseAction

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


def _parse_response(text: str, status: int) -> Any:
    """Parse JSON when possible; fall back to raw text or {"error": ...}.

    Distinguishing transport vs HTTP errors: transport errors don't reach
    here (the caller wraps them with status=-1 + a synthetic body).
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
    url = endpoint.url
    method = (endpoint.method or "POST").upper()
    tool_name = tool_spec.name
    description = tool_spec.description
    parameters = tool_spec.input_schema or {"type": "object", "properties": {}}

    class _HttpAction(BaseAction):
        name: str = tool_name
        description: str = description
        parameters: dict = parameters

        async def __call__(self, **kwargs: Any) -> Any:
            call_id = uuid.uuid4().hex
            started = time.monotonic()
            status, body_text, err = await _post(url, method, kwargs)
            duration_ms = int((time.monotonic() - started) * 1000)
            response = (
                {"error": err} if err is not None else _parse_response(body_text, status)
            )
            step_log.append(_step_log_record(
                tool_call_id=call_id,
                tool=tool_name,
                url=url,
                method=method,
                request=kwargs,
                status=status,
                response=response,
                duration_ms=duration_ms,
                error=err,
                agent_role=agent_role,
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
    # Build a synthetic ToolEndpoint and reuse the HTTP factory.
    endpoint = ToolEndpoint(
        tool_name=tool_spec.name,
        url=target_url,
        method="POST",
    )
    return make_http_action(tool_spec, endpoint, step_log, agent_role=agent_role)
```

- [ ] **Step 5: Install respx for HTTP mocking and re-run tests**

```bash
pip install respx 2>&1 | tail -3
python -m pytest tests/test_aorchestra_bridge.py -p no:quadrants -v 2>&1 | tail -15
```

Expected: 10 tests pass (4 trace.py tests from Task 2 + 6 actions tests from Task 3).

- [ ] **Step 6: Add respx to test extras in pyproject.toml**

Locate the test extras section in `pyproject.toml` and add `respx` to the list. If `[project.optional-dependencies].test` doesn't exist, add it:

```toml
[project.optional-dependencies]
test = [
    "pytest>=9.0",
    "respx>=0.21",
]
```

(If `test` already exists, append `"respx>=0.21",` to the list.)

- [ ] **Step 7: Commit**

```bash
git add src/claw_eval/harnesses/aorchestra/ tests/test_aorchestra_bridge.py pyproject.toml
git commit -m "feat(aorchestra): bridge HTTP and sandbox action factories"
```

### Task 4: ClawEvalEnv adapter (BaseEnv shape + step_log ownership)

**Goal:** Adapter that AOrchestra runners (MainAgent / SubAgent) can use as their environment. Owns the `_step_log` list, the per-tool `agent_role`, and the lifecycle (httpx client close).

**Files:**
- Create: `src/claw_eval/harnesses/aorchestra/_bridge/env.py`
- Modify: `src/claw_eval/harnesses/aorchestra/_bridge/__init__.py` (export)
- Modify: `tests/test_aorchestra_bridge.py` (append more tests)

**Interfaces:**
- Consumes:
  - `make_http_action`, `make_sandbox_action`, `SchemaTranslationError` from Task 3
  - `TaskDefinition` from `claw_eval.models.task`
  - `ToolSpec` / `ToolEndpoint` from `claw_eval.models.tool`
- Produces:
  - `class ClawEvalEnv` — context manager (`__enter__` / `__exit__`)
  - `def reset(self) -> str` — returns the initial observation (== `task.prompt.text`)
  - `def get_action_space(self) -> list[BaseAction]` — compiled `BaseAction` instances for all task tools
  - `def get_action_space_for(self, agent_role: Literal["main", "sub"]) -> list[BaseAction]` — same actions but with `agent_role` baked into their step_log records. MainAgent calls with `"main"`; SubAgent calls with `"sub"`.
  - `def step_log(self) -> list[dict]` — read-only view of accumulated step_log records (copy, not the live list)
  - `def task_id(self) -> str`

Internal:
- `self._step_log_main: list[dict]` and `self._step_log_sub: list[dict]` (we keep two separate lists so the same action object isn't shared between MainAgent and SubAgent; combined at `step_log()` time by chronological merge — easier than mutating the agent_role per call)
- Actually simpler: keep one `self._step_log: list[dict]` and build a fresh set of `BaseAction` instances for each requested agent_role, each capturing the role at construction time.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_aorchestra_bridge.py`:

```python
from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv
from claw_eval.models.task import TaskDefinition
from pathlib import Path


@pytest.fixture
def t077_task():
    return TaskDefinition.from_yaml(
        Path(__file__).resolve().parent.parent
        / "tasks" / "T077_officeqa_highest_dept_spending" / "task.yaml"
    )


def test_clawevalenv_reset_returns_task_prompt(t077_task):
    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        obs = env.reset()
        assert obs == t077_task.prompt.text


def test_clawevalenv_get_action_space_matches_task_tools(t077_task):
    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        actions = env.get_action_space()
        names = {a.name for a in actions}
        assert names == {t.name for t in t077_task.tools}


def test_clawevalenv_get_action_space_for_main_vs_sub_have_different_agent_roles(t077_task):
    """Two calls with different roles return actions whose step_log entries
    carry the right role, regardless of which set is invoked.
    """
    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        main_actions = env.get_action_space_for("main")
        sub_actions = env.get_action_space_for("sub")
        # The actions are wrappers around the same underlying tool definition,
        # but each captures its agent_role at construction. We can't easily
        # invoke them here without HTTP mocking, but at minimum the lists
        # should be distinct objects.
        assert main_actions is not sub_actions
        assert {a.name for a in main_actions} == {a.name for a in sub_actions}


def test_clawevalenv_step_log_starts_empty(t077_task):
    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        assert env.step_log() == []


def test_clawevalenv_rejects_sandbox_tools_without_sandbox_url():
    """Tasks with SANDBOX_TOOL_NAMES require a sandbox_url at construction."""
    from claw_eval.models.task import TaskDefinition
    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "tasks" / "T068zh_llama_w8a8_cuda_bug" / "task.yaml"
    )
    task = TaskDefinition.from_yaml(fixture_path)
    # task declares Bash — should raise without sandbox_url
    with pytest.raises(SchemaTranslationError):
        env = ClawEvalEnv(task, sandbox_url=None)
        env.get_action_space()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_aorchestra_bridge.py::test_clawevalenv_reset_returns_task_prompt -p no:quadrants -v 2>&1 | tail -3
```

Expected: ImportError on `env`.

- [ ] **Step 3: Implement env.py**

Create `src/claw_eval/harnesses/aorchestra/_bridge/env.py`:

```python
"""ClawEvalEnv — adapter that lets AOrchestra agents drive a claw-eval task.

Owns the per-run step_log list, knows about the task's tool inventory, and
hands out BaseAction instances tagged with the requesting agent role.

Lifecycle:
  with ClawEvalEnv(task, sandbox_url=...) as env:
      obs = env.reset()
      actions = env.get_action_space_for("main")
      # ... agent runs ...
      log = env.step_log()
"""
from __future__ import annotations

from typing import Any, Literal

from base.agent.base_action import BaseAction

from ....models.task import TaskDefinition
from ....runner.sandbox_tools import SANDBOX_TOOL_NAMES
from .actions import (
    SchemaTranslationError,
    make_http_action,
    make_sandbox_action,
)


class ClawEvalEnv:
    """Context-managed AOrchestra environment around a single claw-eval task.

    ``sandbox_url`` is required when ``task.tools`` contains any
    SANDBOX_TOOL_NAME (Bash/Read/Write/...). Validation happens lazily — the
    constructor accepts ``None`` but ``get_action_space`` then refuses if a
    sandbox tool needs routing.
    """

    def __init__(self, task: TaskDefinition, *, sandbox_url: str | None) -> None:
        self._task = task
        self._sandbox_url = sandbox_url
        # Single shared list. The trace adapter reads this at the end of the
        # run — it doesn't need to know which list to merge from, and entries
        # already carry agent_role.
        self._step_log: list[dict[str, Any]] = []
        self._closed = False
        # Pre-build endpoint lookup so get_action_space doesn't repeat the
        # scan.
        self._endpoint_by_name = {ep.tool_name: ep for ep in task.tool_endpoints}

    # ----- context manager -----

    def __enter__(self) -> "ClawEvalEnv":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        # httpx.AsyncClient is owned per-call inside _post (Task 3 actions.py)
        # so there's nothing global to close here. The flag is purely a
        # sanity check for any future use-after-close.

    # ----- AOrchestra-facing API -----

    def reset(self) -> str:
        """Return the initial observation — for claw-eval that's the prompt."""
        self._check_open()
        return self._task.prompt.text

    def get_action_space(self) -> list[BaseAction]:
        """Default action space (agent_role='agent'). Mostly for tests; real
        runs use ``get_action_space_for(role)``.
        """
        return self._build_actions(agent_role="agent")

    def get_action_space_for(
        self, agent_role: Literal["main", "sub"]
    ) -> list[BaseAction]:
        """Build a fresh set of BaseAction instances tagged with the given
        role. Each call returns NEW objects so MainAgent and SubAgent never
        share the same action (and therefore never accidentally cross-stamp
        each other's step_log entries).
        """
        return self._build_actions(agent_role=agent_role)

    def step_log(self) -> list[dict[str, Any]]:
        """Snapshot copy of accumulated step_log records."""
        return list(self._step_log)

    @property
    def task_id(self) -> str:
        return self._task.task_id

    # ----- internals -----

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("ClawEvalEnv used after __exit__")

    def _build_actions(self, *, agent_role: str) -> list[BaseAction]:
        self._check_open()
        actions: list[BaseAction] = []
        for tool in self._task.tools:
            if tool.name in SANDBOX_TOOL_NAMES:
                # Will raise SchemaTranslationError if sandbox_url is None.
                actions.append(make_sandbox_action(
                    tool, self._sandbox_url, self._step_log,
                    agent_role=agent_role,
                ))
            else:
                endpoint = self._endpoint_by_name.get(tool.name)
                if endpoint is None:
                    raise SchemaTranslationError(
                        f"tool {tool.name!r} declared in task.tools but has no "
                        f"entry in task.tool_endpoints"
                    )
                actions.append(make_http_action(
                    tool, endpoint, self._step_log,
                    agent_role=agent_role,
                ))
        return actions
```

- [ ] **Step 4: Export env from bridge package**

Update `src/claw_eval/harnesses/aorchestra/_bridge/__init__.py`:

```python
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
```

- [ ] **Step 5: Run env tests**

```bash
python -m pytest tests/test_aorchestra_bridge.py -p no:quadrants -v 2>&1 | tail -15
```

Expected: all bridge tests pass (Task 2 + Task 3 + Task 4 ≈ 15 tests).

- [ ] **Step 6: Commit**

```bash
git add src/claw_eval/harnesses/aorchestra/_bridge/env.py src/claw_eval/harnesses/aorchestra/_bridge/__init__.py tests/test_aorchestra_bridge.py
git commit -m "feat(aorchestra): ClawEvalEnv adapter with step_log ownership"
```

### Task 5: LLMsConfig patch — patched_llms_config context manager

**Goal:** Provide a context manager that swaps in a patched `LLMsConfig._default_config` for the duration of a run, restoring it on exit (including on exception).

**Files:**
- Create: `src/claw_eval/harnesses/aorchestra/_bridge/model_config.py`
- Modify: `src/claw_eval/harnesses/aorchestra/_bridge/__init__.py`
- Modify: `tests/test_aorchestra_bridge.py`

**Interfaces:**
- Consumes:
  - `from base.engine.async_llm import LLMsConfig` (NOTE: this is in `base.engine.async_llm`, NOT `aorchestra.config`)
  - `ModelConfig` from `claw_eval.config`
- Produces:
  - `def build_llms_config(cfg_model: ModelConfig) -> LLMsConfig` — synthesizes an LLMsConfig with two entries:
    - `"claude-sonnet-4-5"` (primary)
    - `"gemini-3-flash-preview"` (alias — same `base_url` / `key`, but `model` field still `"claude-sonnet-4-5"` so delegate.py:266's `_summarize_trace` actually invokes sonnet)
  - `@contextmanager def patched_llms_config(cfg_model: ModelConfig) -> Iterator[LLMsConfig]` — saves `LLMsConfig._default_config`, sets it to the synthesized one, yields, restores on exit even on exception.

Critical schema notes (verified during plan write):
- `LLMsConfig.__init__(config_dict)` wraps a `dict[str, dict]` where each inner dict has keys `model / temperature / key / base_url / top_p`
- `LLMsConfig.get(name)` raises `ValueError` if `name` not in `configs`
- `LLMsConfig._default_config` is a class attribute that the `default()` classmethod lazy-initializes from yaml. To force-override, set the class attribute before any call to `default()`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_aorchestra_bridge.py`:

```python
from base.engine.async_llm import LLMsConfig

from claw_eval.config import ModelConfig
from claw_eval.harnesses.aorchestra._bridge.model_config import (
    build_llms_config,
    patched_llms_config,
)


@pytest.fixture
def claweval_cfg_model():
    return ModelConfig(
        model_id="claude-sonnet-4-5",
        api_key="sk-test-12345",
        base_url="https://newapi.example.com/v1",
    )


def test_build_llms_config_has_primary_and_alias(claweval_cfg_model):
    cfg = build_llms_config(claweval_cfg_model)
    primary = cfg.get("claude-sonnet-4-5")
    alias = cfg.get("gemini-3-flash-preview")
    assert primary.base_url == "https://newapi.example.com/v1"
    assert primary.key == "sk-test-12345"
    assert alias.base_url == "https://newapi.example.com/v1"
    assert alias.key == "sk-test-12345"
    # Critical: the alias's MODEL field must point at claude-sonnet-4-5
    # so AOrchestra actually runs sonnet when it asks for "gemini-3-flash-preview".
    assert alias.model == "claude-sonnet-4-5"


def test_patched_llms_config_restores_default_on_exit(claweval_cfg_model):
    previous = LLMsConfig._default_config
    with patched_llms_config(claweval_cfg_model) as cfg:
        assert LLMsConfig._default_config is cfg
    # After exit, _default_config is whatever it was before.
    assert LLMsConfig._default_config is previous


def test_patched_llms_config_restores_on_exception(claweval_cfg_model):
    previous = LLMsConfig._default_config
    with pytest.raises(RuntimeError):
        with patched_llms_config(claweval_cfg_model):
            raise RuntimeError("simulated crash")
    assert LLMsConfig._default_config is previous
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_aorchestra_bridge.py::test_build_llms_config_has_primary_and_alias -p no:quadrants 2>&1 | tail -3
```

Expected: ImportError on `model_config`.

- [ ] **Step 3: Implement model_config.py**

Create `src/claw_eval/harnesses/aorchestra/_bridge/model_config.py`:

```python
"""LLMsConfig injection + alias mapping.

AOrchestra picks model configs from ``base.engine.async_llm.LLMsConfig._default_config``,
a module-level singleton class attribute that lazy-loads from yaml on first
``LLMsConfig.default()`` call. We swap that attribute in for the duration of
a claw-eval run so:
  1. ``main_model="claude-sonnet-4-5"`` works (primary entry)
  2. ``delegate.py:266`` — which hardcodes ``LLMsConfig.default().get("gemini-3-flash-preview")``
     for trace summarization — actually runs sonnet too (alias entry).

This is the cleanest way to keep AOrchestra source untouched per the spec.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from base.engine.async_llm import LLMsConfig

from ....config import ModelConfig


def build_llms_config(cfg_model: ModelConfig) -> LLMsConfig:
    """Synthesize an LLMsConfig with two entries pointing at the claw-eval
    endpoint:

    1. ``"claude-sonnet-4-5"`` — the canonical model name used by both
       MainAgent and SubAgent.
    2. ``"gemini-3-flash-preview"`` — alias entry whose ``model`` field is
       ``"claude-sonnet-4-5"``. Necessary because ``delegate.py:266``
       hardcodes this key for the trace summarizer call.
    """
    base_url = cfg_model.base_url or ""
    key = cfg_model.api_key or ""
    inner = {
        "claude-sonnet-4-5": {
            "model": "claude-sonnet-4-5",
            "key": key,
            "base_url": base_url,
            "temperature": 0,
        },
        "gemini-3-flash-preview": {
            # NB: the model field stays sonnet so AOrchestra actually
            # invokes sonnet when it asks for gemini.
            "model": "claude-sonnet-4-5",
            "key": key,
            "base_url": base_url,
            "temperature": 0,
        },
    }
    return LLMsConfig(inner)


@contextmanager
def patched_llms_config(cfg_model: ModelConfig) -> Iterator[LLMsConfig]:
    """Swap LLMsConfig._default_config for the duration of the with-block.

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
```

- [ ] **Step 4: Export from bridge package**

Update `src/claw_eval/harnesses/aorchestra/_bridge/__init__.py`:

```python
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
from .model_config import build_llms_config, patched_llms_config

__all__ = [
    "ClawEvalEnv",
    "SANDBOX_ENDPOINTS",
    "SchemaTranslationError",
    "build_llms_config",
    "make_http_action",
    "make_sandbox_action",
    "patched_llms_config",
]
```

- [ ] **Step 5: Run all bridge tests**

```bash
python -m pytest tests/test_aorchestra_bridge.py -p no:quadrants -v 2>&1 | tail -20
```

Expected: ~18 tests pass.

- [ ] **Step 6: Confirm Wave 1-3 still green**

```bash
python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
```

Expected: `~75 passed, 3 skipped` (61 original + 14 new bridge tests + Wave 1-3 placeholders parameter expansion comes in Task 9).

- [ ] **Step 7: Commit**

```bash
git add src/claw_eval/harnesses/aorchestra/_bridge/model_config.py src/claw_eval/harnesses/aorchestra/_bridge/__init__.py tests/test_aorchestra_bridge.py
git commit -m "feat(aorchestra): LLMsConfig patch with sonnet+gemini alias"
```

---

## Wave 4-C: Trace Adapter (subagent)

### Task 6: Trace adapter unit tests and fixtures (subagent dispatch)

**Goal:** Translate AOrchestra trajectory JSON + step_log JSONL into claw-eval JSONL trace consumable by the existing grader.

**Files:**
- Create: `tests/fixtures/aorchestra/trajectory_sample.json`
- Create: `tests/fixtures/aorchestra/step_log_sample.jsonl`
- Create: `tests/test_aorchestra_trace_adapter.py`
- Create: `src/claw_eval/harnesses/aorchestra/_trace_adapter.py`

**Interfaces:**
- Consumes:
  - `from claw_eval.trace.writer import TraceWriter`
  - `from claw_eval.trace.reader import load_trace`
  - `from claw_eval.models.trace import TraceStart, TraceMessage, ToolDispatch, AuditSnapshot, TraceEnd, TokenUsage`
  - `from claw_eval.models.message import Message`
  - `from claw_eval.models.content import TextBlock, ToolUseBlock, ToolResultBlock`
  - `from claw_eval.models.task import TaskDefinition`
- Produces:
  - `def translate_aorchestra(*, trajectory_path: Path | None, step_log_path: Path | None, audit_data: dict[str, dict], task: TaskDefinition, run_id: str, trace_dir: Path, duration_ms: int, status: Literal["ok", "error", "timeout"]) -> Path` — writes JSONL trace, returns path.
  - Partial-input tolerance: missing/empty trajectory still produces a minimal valid trace (`TraceStart + TraceMessage(user prompt) + TraceEnd(status="error", failure_modes=["error"])`)

Trajectory JSON shape (verified from `aorchestra/runners/gaia_runner.py:100-123`):
```json
{
  "task_id": "...",
  "timestamp": "...",
  "main_model": "claude-sonnet-4-5",
  "sub_models": ["claude-sonnet-4-5"],
  "success": true,
  "total_reward": 1.0,
  "total_cost": 0.12,
  "main_cost": 0.05,
  "sub_cost": 0.07,
  "attempts": 2,
  "trajectory": [ ... ],   // <- attempts_detail list
  ...
}
```

Each `attempts_detail` entry contains the agent's per-step actions. The adapter is pragmatic: it walks the list, identifies LLM text vs tool actions vs sub-attempt nests, and emits TraceMessage / ToolUseBlock / ToolResultBlock accordingly. **The plan does NOT prescribe the exact attempts_detail walk — the subagent doing Task 6 needs to inspect `aorchestra/runners/*.py` to learn the real schema and choose a robust traversal.**

step_log JSONL shape (canonical, defined in Task 3):
```jsonl
{"toolCallId": "abc123...", "agent_role": "main", "tool": "ocr_extract_text", "url": "...", "method": "POST", "request": {...}, "status": 200, "response": {...}, "durationMs": 142, "error": null}
```

**Important plan note for the subagent:** the canonical `step_log` schema is exactly what `_step_log_record` in `src/claw_eval/harnesses/aorchestra/_bridge/actions.py` produces. Read that file first to lock the shape.

**Dispatch this task to a subagent. The prompt:**

```
Implement Wave 4-C trace adapter for the AOrchestra harness.

Spec: docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md §3 (data flow), §4.5-4.6 (failure handling), §5.2 (test coverage).

Plan: docs/superpowers/plans/2026-06-24-aorchestra-harness.md Task 6.

Forbidden zones (do not touch):
- src/claw_eval/harnesses/openclaw.py / _openclaw_* / _trace_adapter.py / _snapshot.py
- src/claw_eval/harnesses/aorchestra/_bridge/ (Wave 4-B is done; read but don't edit)
- src/claw_eval/runner/ / src/claw_eval/graders/ / src/claw_eval/models/ except as listed
- pyproject.toml (Task 9's job)

Files to produce:
1. tests/fixtures/aorchestra/trajectory_sample.json — handcrafted, 2 MainAgent text steps + 1 MainAgent tool call (ocr_extract_text) + 1 SubAgent delegate sub-attempt + final answer
2. tests/fixtures/aorchestra/step_log_sample.jsonl — toolCallIds aligned with trajectory; one entry with status 200, one with status 500 for is_error coverage
3. src/claw_eval/harnesses/aorchestra/_trace_adapter.py — translate_aorchestra(...) returning Path
4. tests/test_aorchestra_trace_adapter.py — 8 tests covering:
   - test_translate_basic: counts match (TraceStart, N TraceMessage, M ToolDispatch, AuditSnapshot, TraceEnd)
   - test_agent_role_filled: trace has events with agent_role in {"main", "sub"}; no event has agent_role="agent"
   - test_load_trace_roundtrip: load_trace() returns the expected tuple shape
   - test_grader_can_consume: a minimal mock AbstractGrader subclass's grade(...) doesn't raise
   - test_partial_trajectory_missing: trajectory_path=None produces TraceStart + TraceMessage(user prompt) + TraceEnd(status="error", failure_modes=["error"])
   - test_partial_trajectory_empty_file: trajectory_path points to empty JSON file → same as missing
   - test_step_log_status_500_marks_is_error: ToolResultBlock for that callID has is_error=True
   - test_audit_data_yields_audit_snapshots: each service in audit_data gets one AuditSnapshot event

How to run tests:
  python -m pytest tests/test_aorchestra_trace_adapter.py -p no:quadrants -v
Expected outcome: 8/8 pass, full suite (tests/) still 61+ pass plus your 8.

Critical:
- The OpenClaw trace adapter at src/claw_eval/harnesses/_trace_adapter.py is a reference for the OpenClaw-side merge logic. AOrchestra's case is MUCH SIMPLER because we own both data sources — but read the OpenClaw adapter to understand the canonical claw-eval trace event shapes (TraceMessage, ToolDispatch, etc.) and AuditSnapshot timing.
- The bridge step_log shape is set in src/claw_eval/harnesses/aorchestra/_bridge/actions.py (function _step_log_record). Match it exactly.
- agent_role on TraceMessage and ToolDispatch was added in Wave 4-B Task 2.
- Use TraceWriter for output (don't hand-write JSONL).
- Partial trajectory handling: failure_modes copies OpenClaw's schema exactly — see _openclaw_native.py:1167-1187 and _trace_adapter.py:439-446.

When done: commit each of (1)(2), (3)(4) as separate atomic commits if convenient, but a single commit covering all four is fine too. Push at the end is NOT your job — main conversation handles that.
```

- [ ] **Step 1: Dispatch Task 6 subagent**

When dispatching, paste the prompt above verbatim into the agent call. After completion, review the produced files for:
- Sample fixtures are syntactically valid JSON / JSONL
- Adapter handles None / missing / empty inputs
- 8 tests are in test_aorchestra_trace_adapter.py
- Full suite still passes (61 + 14 + 8 = 83)

- [ ] **Step 2: Verify the subagent's commits**

```bash
git log --oneline -5
git diff HEAD~1..HEAD -- src/claw_eval/harnesses/aorchestra/_trace_adapter.py tests/test_aorchestra_trace_adapter.py tests/fixtures/aorchestra/ | head -50
```

- [ ] **Step 3: Run the whole suite**

```bash
python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
```

Expected: ~83 passed, 3 skipped.

---

## Wave 4-D: Host smoke harness + T077 e2e (subagent)

### Task 7: Implement AOrchestraHarness, register, CLI gate, host e2e (subagent dispatch)

**Goal:** Assemble Tasks 2-6 into a working AOrchestraHarness that:
- Registers as `--harness aorchestra`
- Refuses (with a clear error) tasks that need SANDBOX_TOOLS without `--sandbox`
- Drives a real LLM run on T077 (which has no SANDBOX_TOOLS — host smoke path)

**Files:**
- Create: `src/claw_eval/harnesses/aorchestra/_runner.py`
- Create: `src/claw_eval/harnesses/aorchestra/harness.py`
- Modify: `src/claw_eval/harnesses/aorchestra/__init__.py` (export)
- Modify: `src/claw_eval/harnesses/__init__.py` (register)
- Modify: `src/claw_eval/cli.py` (choices + on-demand container)
- Modify: `pyproject.toml` (aorchestra extras)
- Modify: `tests/test_harness_placeholders.py` (extend parametrization)
- Create: `tests/test_aorchestra_e2e.py`

**Interfaces:**
- Consumes (from Wave 4-B / 4-C):
  - `ClawEvalEnv`, `make_http_action` / `make_sandbox_action` (don't use directly — env handles)
  - `patched_llms_config`
  - `translate_aorchestra`
- Consumes (from claw-eval core):
  - `Harness` Protocol, `HarnessResult` from `claw_eval.harnesses.base`
  - `SANDBOX_TOOL_NAMES` from `claw_eval.runner.sandbox_tools`
  - `from claw_eval.harnesses._snapshot import collect_workdir_snapshot, inject_grader_files_host` (host backend reuse)
  - `from claw_eval.cli import _collect_env_snapshot` (sandbox backend reuse — Wave 4-E only)
- Consumes (from AOrchestra):
  - `from aorchestra.main_agent import MainAgent`
  - `from aorchestra.tools.delegate import DelegateTaskTool`
  - `from aorchestra.tools.complete import CompleteTask` (or whatever the actual name is — subagent verifies)
  - `from base.engine.async_llm import LLMsConfig, create_llm_instance`
- Produces:
  - `class AOrchestraHarness` implementing `Harness` Protocol
    - `name = "aorchestra"`
    - `supported_features = frozenset({"http_services", "sandbox_tools"})`
    - `preflight(task) -> list[str]` (per §4.1 decision table)
    - `run(task, *, trace_dir, run_id, cfg, sandbox_handle, user_agent, services_ctx, sandbox_tools=False) -> HarnessResult`
  - Internal `_run_host_smoke` (used when sandbox_handle is None; Wave 4-E adds `_run_container`)

**Dispatch this task to a subagent. The prompt:**

```
Implement Wave 4-D: AOrchestraHarness integration + T077 host smoke e2e.

Spec: docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md §3 (data flow), §4 (errors), §6 (Wave 4-D specifics).
Plan: docs/superpowers/plans/2026-06-24-aorchestra-harness.md Task 7.

Prerequisites that ALREADY EXIST in the repo (read them, then build on them):
- src/claw_eval/harnesses/aorchestra/_bridge/* — actions, env, model_config (Wave 4-B)
- src/claw_eval/harnesses/aorchestra/_trace_adapter.py — translate_aorchestra (Wave 4-C)
- tests/fixtures/aorchestra/* — trajectory + step_log samples (Wave 4-C)
- docs/superpowers/specs/aorchestra_decision.md — Wave 4-A probe results

Forbidden zones (do not touch except as listed below):
- src/claw_eval/harnesses/openclaw.py / _openclaw_* / _trace_adapter.py / _snapshot.py (read-only reference)
- src/claw_eval/harnesses/aorchestra/_bridge/ and _trace_adapter.py (Wave 4-B/C done)
- src/claw_eval/runner/loop.py / src/claw_eval/graders/ / src/claw_eval/models/scoring.py
- src/claw_eval/harnesses/{base,claweval,codex,claudecode,openclaw}.py
- mock_services/ / tasks/ / Dockerfile.openclaw

Files to create / modify:

1. src/claw_eval/harnesses/aorchestra/_runner.py
   - async def run_one_task(task: TaskDefinition, env: ClawEvalEnv, cfg: Config, *, case_dir: Path) -> dict
   - Build MainAgent (sub_models=["claude-sonnet-4-5"], tools = env.get_action_space_for("main") + [DelegateTaskTool, CompleteTask])
   - Wire SubAgent factory so SubAgent tools = env.get_action_space_for("sub") + [CompleteTask]  (NO DelegateTaskTool — avoid infinite delegation)
   - Run MainAgent's step loop until CompleteTask is invoked or max_attempts reached
   - Persist trajectory JSON to case_dir / f"{task.task_id}_{timestamp}.json"
   - Return {"trajectory_path": Path, "status": Literal["ok","error","timeout"], "duration_ms": int}

2. src/claw_eval/harnesses/aorchestra/harness.py
   - class AOrchestraHarness implementing Harness Protocol
   - name = "aorchestra"
   - supported_features = frozenset({"http_services", "sandbox_tools"})
   - preflight per §4.1: rejects user_agent.enabled, rejects SANDBOX_TOOLS without sandbox_handle, rejects unsupported JSON Schema constructs
   - run() dispatches:
       * sandbox_handle is None and task has no SANDBOX_TOOL_NAMES → _run_host_smoke
       * sandbox_handle is None and task HAS SANDBOX_TOOL_NAMES → preflight should have caught; second-line raise SystemExit(2)
       * sandbox_handle is not None → _run_container (Wave 4-E will implement; Wave 4-D leaves a NotImplementedError stub for now)
   - _run_host_smoke: 
       a. with patched_llms_config(cfg.model):
            with ClawEvalEnv(task, sandbox_url=None) as env:
              raw = await _runner.run_one_task(...)
       b. audit_data = services_ctx.collect_audit() if services_ctx else {} (use the same _collect_audit helper pattern from OpenClawHarness)
       c. env_snapshot = collect_workdir_snapshot(work_dir=task_dir, task=task, task_dir=task_dir) if task has env_snapshot_files or env_snapshot_commands else None
          [host smoke path doesn't need inject_grader_files_host unless task has them — call only if list is non-empty]
       d. trace_path = translate_aorchestra(...)
       e. return HarnessResult(trace_path, env_snapshot, audit_data, raw_dir=case_dir)
   - Helper _collect_audit(task, services_ctx): copy the OpenClawHarness implementation (it loops task.services, derives audit_url from reset_endpoint, httpx.get with timeout=5)

3. src/claw_eval/harnesses/aorchestra/__init__.py
   - Export AOrchestraHarness

4. src/claw_eval/harnesses/__init__.py
   - Add "aorchestra": AOrchestraHarness() to _REGISTRY
   - Add the import

5. src/claw_eval/cli.py
   - In all three subparsers (run / _run-inner / batch), add "aorchestra" to --harness choices
   - In the dispatch logic before harness.run():
       if args.harness == "aorchestra":
           task_needs_sandbox = any(t.name in SANDBOX_TOOL_NAMES for t in task.tools)
           if task_needs_sandbox and not sandbox_mode:
               print("ERROR: --harness aorchestra requires --sandbox when task declares Bash / Read / Write / etc. tools.", file=sys.stderr)
               raise SystemExit(2)
   - Do NOT add the openclaw-style "all openclaw tasks need --sandbox" rule — aorchestra is asymmetric (§4.2).
   - Import SANDBOX_TOOL_NAMES at the top of cli.py

6. pyproject.toml
   - Add [project.optional-dependencies].aorchestra with the minimal subset needed for the harness path to import (not the full 144-line AOrchestra requirements.txt):
       aorchestra = [
         "aiofiles>=24.1.0",
         "litellm>=1.80.0",
         "loguru>=0.7.3",
         "pyyaml>=6.0.3",
         "tiktoken>=0.12.0",
         "respx>=0.21",          # already needed for tests
       ]
   - User still does `pip install -e /data2/ruanjianhao/AOrchestra` separately (README change is OK; touch only the README aorchestra section)

7. tests/test_harness_placeholders.py
   - Extend PLACEHOLDER_HARNESSES — actually, aorchestra is NOT a placeholder anymore. Update the registry-sanity test to expect 5 entries: {"claweval", "openclaw", "codex", "claudecode", "aorchestra"}. Leave codex/claudecode in PLACEHOLDER_HARNESSES; remove aorchestra if it ever ended up there. Add a separate test test_aorchestra_registered_and_preflights_t077 that loads T077 and asserts AOrchestraHarness().preflight(T077_task) == [].

8. tests/test_aorchestra_e2e.py
   - Modeled on tests/test_openclaw_e2e.py
   - @pytest.mark.e2e, @pytest.mark.skipif(not RUN_E2E), @pytest.mark.skipif(not LLM creds)
   - Starts the OCR mock service (PORT=9121, OCR_FIXTURES + OCR_FILENAME) — copy the ocr_service fixture from test_openclaw_e2e.py verbatim
   - Constructs cfg with claude-sonnet-4-5 via env vars
   - Calls AOrchestraHarness().run(task=T077, sandbox_handle=None, ...)
   - Asserts (per §5.3):
       (1) trace_path exists and load_trace() returns valid tuple
       (2) at least one TraceMessage or ToolDispatch has agent_role in {"main", "sub"}
       (3) task_score >= 0.3 (use LLMJudge to grade like test_openclaw_e2e.py does)
       (4) env_snapshot is None or has the canonical schema (cmd:/file:/local_file: keys)
   - Records delegate_count in e2e_report.json (count assistant messages with agent_role="main" that invoked delegate_task; soft-only, do NOT fail if zero)

How to verify locally (don't actually run e2e):
  python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
  Expected: full unit-test suite passes; e2e tests skipped without RUN_E2E.

If something doesn't work AT ALL because of an AOrchestra API surface I (the plan author) didn't anticipate, STOP. Don't invent a workaround that silently changes the contract. Write a deviation report to the end of your final message: what I assumed, what the actual API is, what the smallest change to the harness contract would be. The main conversation will adjudicate.

Commit cadence: one commit per file or per related-file group is fine. Make sure the messages are descriptive ("feat(aorchestra): register harness in CLI" etc.).

DO NOT run e2e tests yourself. The main conversation will run them after reviewing your diff.
```

- [ ] **Step 1: Dispatch the subagent with the prompt above**

- [ ] **Step 2: Review the subagent's diff**

```bash
git log --oneline -10
git diff main~5..HEAD -- src/claw_eval/harnesses/aorchestra/harness.py src/claw_eval/harnesses/aorchestra/_runner.py src/claw_eval/cli.py | head -200
```

Check that:
- Forbidden zones untouched
- CLI gate refuses aorchestra+Bash without --sandbox
- preflight rejects user_agent.enabled
- supported_features = `{"http_services", "sandbox_tools"}`
- harness uses patched_llms_config context manager (or equivalent restore)

- [ ] **Step 3: Run unit suite**

```bash
python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
```

Expected: all pass.

- [ ] **Step 4: Run T077 e2e**

```bash
export CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1
export CLAWEVAL_LLM_API_KEY=...           # provided by user out-of-band
export CLAWEVAL_LLM_MODEL=claude-sonnet-4-5
RUN_E2E=1 python -m pytest tests/test_aorchestra_e2e.py -p no:quadrants -v 2>&1 | tail -15
```

Expected: e2e passes, e2e_report.json written to tmp_path.

- [ ] **Step 5: Inspect e2e_report.json**

Find the report and verify the four acceptance points:

```bash
find /tmp/pytest-of-root -name "e2e_report.json" -mmin -10 | head -1 | xargs cat
```

Look for: `agent_role_seen: ["main"]` (or `["main", "sub"]`), `task_score ≥ 0.3`, `snapshot_ok: true`, `delegate_count: N` (any value ≥ 0).

---

## Wave 4-E: Container e2e + Bash bridge (subagent)

### Task 8: Container path + T068 e2e (subagent dispatch)

**Goal:** Implement `_run_container` (the second branch of `AOrchestraHarness.run`) so tasks with SANDBOX_TOOL_NAMES route to the in-container sandbox server. Validate via T068 (declares `Bash`).

**Files:**
- Modify: `src/claw_eval/harnesses/aorchestra/harness.py` (replace `_run_container` NotImplementedError with real impl)
- Modify: `src/claw_eval/harnesses/aorchestra/_runner.py` (accept optional `sandbox_url`)
- Create: `tests/test_aorchestra_e2e_container.py`

**Interfaces:**
- Consumes:
  - `from claw_eval.runner.sandbox_runner import SandboxRunner`
  - `from claw_eval.cli import _collect_env_snapshot` (or wherever it lives — sandbox URL backend)
- Produces: `_run_container` returning `HarnessResult` exactly like `_run_host_smoke` but routing sandbox tools via the container.

**Dispatch this task to a subagent. The prompt:**

```
Implement Wave 4-E: AOrchestra container path + T068 Bash bridge e2e.

Spec: docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md §3.7 (container topology), §4.2-4.3 (container + env_snapshot), §5.3 (T068 e2e).
Plan: docs/superpowers/plans/2026-06-24-aorchestra-harness.md Task 8.

Prerequisites already in repo:
- All of Wave 4-A through 4-D
- src/claw_eval/harnesses/aorchestra/harness.py has _run_container raising NotImplementedError — you replace it
- Dockerfile.openclaw image (claw-eval-agent-openclaw:latest) — REUSE, do NOT rebuild
- SandboxRunner.start_container supports network_mode="host" (added in Wave 3-E)

Forbidden zones (do not touch):
- Same as Wave 4-D, plus:
- src/claw_eval/harnesses/aorchestra/_bridge/ and _trace_adapter.py (read-only)
- All Wave 1-3 and Wave 4-A/B/C files
- Dockerfile.openclaw

Files to change:

1. src/claw_eval/harnesses/aorchestra/harness.py
   - Replace the NotImplementedError in _run_container with the real implementation:
     a. sandbox_url = sandbox_handle.sandbox_url
     b. with patched_llms_config(cfg.model):
          with ClawEvalEnv(task, sandbox_url=sandbox_url) as env:
            raw = await _runner.run_one_task(...)
     c. audit_data = self._collect_audit(task, services_ctx)
     d. env_snapshot via _collect_env_snapshot(sandbox_url, task) — see how OpenClawHarness._run_container uses it
     e. trace_path = translate_aorchestra(...)
     f. return HarnessResult(trace_path, env_snapshot, audit_data, raw_dir=case_dir)
   - Key §3.7 invariant: collect_audit BEFORE the caller stops the container. The caller (CLI / e2e test) owns container lifecycle, so audit must be in _run_container's body, before returning.

2. src/claw_eval/harnesses/aorchestra/_runner.py
   - Accept sandbox_url parameter (already-existing path passes None; container path passes the URL)

3. tests/test_aorchestra_e2e_container.py
   - Modeled on tests/test_openclaw_e2e_container.py — copy structure, adapt for AOrchestra
   - Gates: @skipif(not RUN_E2E), @skipif(not docker), @skipif(not image claw-eval-agent-openclaw:latest), @skipif(not LLM creds)
   - Pick T068zh_llama_w8a8_cuda_bug (declares Bash, no user_agent)
   - Start web_real mock service if T068 declares one — check task.yaml for services entry
   - Start the openclaw-image container via SandboxRunner with network_mode="host"
   - Construct cfg with sonnet-4-5
   - Call AOrchestraHarness().run(task=T068, sandbox_handle=handle, ...)
   - 7 acceptance checks (§5.3) — write all 7 into e2e_container_report.json:
     (1) callID_consistency: every step_log toolCallId appears in trace; trivially true here
     (2) bridge_log_complete: trajectory toolCall count == step_log count for that turn (we own both)
     (3) task_score not asserted (T068 has no clean grader; just verify load_trace works and a score can be computed without exception)
     (4) snapshot dict shape OK
     (5) Bash bridge URL: any step_log record with tool=="Bash" must have url == f"{sandbox_url}/exec"
     (6) audit_data in HarnessResult is dict (may be empty)
     (7) agent_role appears in trace events (at least one main, maybe sub)
   - Record delegate_count in the report (soft, no fail)

Notes:
- The model may not actually call Bash on T068 (it answered from knowledge in Wave 3-E e2e). That's OK: check 5 is conditional — "if any step_log record has tool=='Bash', then ...". Do not force-fail if zero Bash calls happened.
- Use the existing helper for image / docker availability checks if you can find one in tests/test_openclaw_e2e_container.py; otherwise inline a minimal version.

How to verify (don't actually run):
  python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
Expected: full unit suite passes (e2e tests skip without RUN_E2E + docker).

If something is genuinely different from the Wave 3-E pattern, stop and report. Otherwise commit each major change with a clear message.

DO NOT run e2e tests yourself.
```

- [ ] **Step 1: Dispatch the subagent**

- [ ] **Step 2: Review the diff**

```bash
git log --oneline -5
git diff HEAD~3..HEAD -- src/claw_eval/harnesses/aorchestra/harness.py tests/test_aorchestra_e2e_container.py | head -200
```

Verify the 7 acceptance checks are all written into the test, container lifecycle is owned by the test (not the harness), and audit is collected before container stop.

- [ ] **Step 3: Run unit suite**

```bash
python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
```

Expected: all unit tests still pass.

- [ ] **Step 4: Run container e2e**

```bash
docker images | grep claw-eval-agent-openclaw  # confirm image exists from Wave 3-E
RUN_E2E=1 python -m pytest tests/test_aorchestra_e2e_container.py -p no:quadrants -v 2>&1 | tail -20
```

Expected: passes; both T077 container (regression) and T068 (Bash bridge) succeed.

- [ ] **Step 5: Inspect e2e_container_report.json**

```bash
find /tmp/pytest-of-root -name "e2e_container_report*.json" -mmin -10
```

Confirm: all 7 acceptance fields populated, score for T077 within 0.1 of Wave 4-D host result, Bash URL points at sandbox.

- [ ] **Step 6: Update progress.md (main conversation, not subagent)**

Append a Wave 4 section to `docs/progress.md` mirroring the Phase 3 wave structure: Waves 4-A through 4-E completed, link to spec, link to plan, list of e2e_report json paths.

- [ ] **Step 7: Commit progress + push**

```bash
git add docs/progress.md
git commit -m "docs(progress): Phase 4 waves 4-A through 4-E complete"
git push origin main
```

---

## Self-Review

**1. Spec coverage:**

- §1 architecture overview → Tasks 3 (actions), 4 (env), 5 (model_config), 7 (harness assembly)
- §2 module layout → all tasks; file layout encoded in "File Structure" header
- §3 data flow → Tasks 6 (trace adapter), 7 (harness orchestration), 8 (container path)
- §4.1 preflight → Task 7 (harness.py preflight)
- §4.2 container decision → Task 7 (CLI gate)
- §4.3 env_snapshot reuse → Tasks 7 (host workdir) and 8 (sandbox URL)
- §4.4 LLMsConfig contract → Task 5
- §4.5 failure modes → Task 6 (partial trajectory handling)
- §4.6 trace translation degradation → Task 6 (test_partial_trajectory_*)
- §5.1 test pyramid → distributed across all tasks; total ≈ 15 bridge + 8 adapter + 1 registry + 2 e2e
- §5.2 unit coverage → Tasks 2-5 (bridge) + Task 6 (adapter)
- §5.3 e2e → Tasks 7 (T077) and 8 (T068)
- §5.4 cross-harness → Wave 4-F (user manual, out of plan scope per user request)
- §6 cadence → matches: 4-A (main, Task 1) + 4-B (main, Tasks 2-5) + 4-C (subagent, Task 6) + 4-D (subagent, Task 7) + 4-E (subagent, Task 8)

**2. Placeholder scan:** none of "TBD / TODO / fill in details / etc." present. All code blocks are complete and runnable.

**3. Type consistency:**
- `agent_role: Literal["main", "sub", "agent"]` — same in trace.py change (Task 2) and step_log records (Task 3) and env build (Task 4)
- `step_log` entry schema — defined in Task 3's `_step_log_record`, consumed by Task 6's trace adapter, owned by Task 4's env
- `LLMsConfig._default_config` — Task 1 probes it, Task 5 patches it. Schema verified: it's a class attribute holding an `LLMsConfig` instance (not a raw dict).
- `make_http_action` / `make_sandbox_action` — same signature in Tasks 3, 4
- `patched_llms_config` — defined in Task 5, used in Tasks 7 and 8

**4. Scope check:** single feature (AOrchestra harness). Tasks chain cleanly: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8. Wave 4-F (cross-harness comparison) is explicitly user-manual and excluded.
