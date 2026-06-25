# AOrchestra Pluggable SubAgent Runtime (Phase 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AOrchestra's hardcoded `DelegateTaskTool → ReActAgent` dispatch with a pluggable `SubAgentRuntime` interface, then add a `PiRuntime` that drives a Node-side `@earendil-works/pi-agent-core` worker over JSON-RPC stdio. Validate end-to-end by re-running claw-eval T077 through Pi and confirming the e2e acceptance bar.

**Architecture:** AOrchestra's `〈I, C, T, M〉` four-tuple is the intermediate representation; the runtime is the execution backend. We carve the seam at `DelegateTaskTool → runner.run(sub_agent, env)` rather than at `agent.step()` so a Pi worker that already owns its own tool-call loop never gets wrapped in AO's loop. ReActRuntime is the wrapper that preserves the current behaviour; PiRuntime spawns a Node subprocess per delegated task and routes Pi's tool calls back to AO's `Environment.step()` via JSON-RPC.

**Tech Stack:** Python 3.11+, pydantic 2.x, AOrchestra repo at `/data2/ruanjianhao/AOrchestra/`, Node 22+, `@earendil-works/pi-agent-core@0.80.2` + `@earendil-works/pi-ai@0.80.2` (npm), `typebox` for tool schemas, JSON-RPC 2.0 over stdio.

## Global Constraints

- **Implementation site is AOrchestra**, not claw-eval. Almost every file path in this plan begins with `/data2/ruanjianhao/AOrchestra/`. claw-eval is touched in exactly one place: `src/claw_eval/harnesses/aorchestra/_runner.py` switches to pass `runtime_name="pi"` at the end (Task 11). Everything else in claw-eval (`_bridge/`, `_trace_adapter.py`, e2e tests, fixtures) stays as-is.
- **Don't change AOrchestra's `〈I,C,T,M〉` contract.** Runtime selection is *execution infrastructure*, not part of the four-tuple. It's plumbed as a config field (`subagent_runtime: "react" | "pi"`), not as a fifth tuple member.
- **No loop-in-loop.** Pi owns the sub-agent step loop end-to-end. AO's `Runner.run()` is bypassed in the Pi path. The seam is `DelegateTaskTool → runtime.run(spec, env)`, not `runtime.step()`.
- **Step budget enforced server-side.** Pi's max_steps is enforced inside the Python tool-gateway, not relied on from the Pi-side prompt. Once `steps >= spec.max_steps`, the next `tool_call` returns `{"done": true, "termination_reason": "max_steps"}` and Pi terminates.
- **Pi's built-in tools are forbidden.** Every Node session is created with `noTools: "builtin"` (or for `pi-agent-core` directly: only the explicitly-passed `tools` list). The model never sees `bash` / `read` / `write` / `edit` — only AO-Environment-backed tools.
- **All tool execution is sequential.** Both at the Pi `Agent` level (`toolExecution: "sequential"`) and per-tool (`executionMode: "sequential"`). AO Environment state is not concurrency-safe.
- **`env.reset()` semantics match `benchmark/common/runner.py:Runner.run()` lines 55-59** — `agent.reset(info)` then `env.reset()`. PiRuntime calls these in the same order before the first tool call.
- **Trace schema is AO `StepRecord`**, never raw Pi transcript. PiRuntime converts every Pi `tool_execution_end` event into a `StepRecord(observation, action, reward, raw_response, done, info)` before returning. Existing trace formatters and the claw-eval `_trace_adapter` continue to consume the same dict shape.
- **AOrchestra source mods continue the "decision-9 style" precedent** — direct edits to `/data2/ruanjianhao/AOrchestra/`. There is no upstream PR. The architectural diff is registered in `/data2/ruanjianhao/claw-eval/docs/superpowers/specs/aorchestra_decision.md` (Task 14).
- **AORCHESTRA_ROOT env var override** continues to work: `/data2/ruanjianhao/AOrchestra` is the default but tests should respect `os.environ.get("AORCHESTRA_ROOT", ...)`.
- **PI_RUNTIME_NODE_BIN env var override** for the node binary path, default `node`.
- **TDD discipline.** Failing test → minimal implementation → passing test → commit. Each task ends with a runnable test command and an `Expected:` line.

---

## File Structure

### New (inside AOrchestra)

```
/data2/ruanjianhao/AOrchestra/
├── aorchestra/runtime/
│   ├── __init__.py                  # Public surface: SubAgentSpec, SubAgentRunResult, SubAgentRuntime, RuntimeRegistry
│   ├── base.py                      # Dataclasses + Protocol + registry impl
│   ├── react_runtime.py             # Wraps the existing ReActAgent + Runner code path
│   └── pi_runtime.py                # Spawns Node worker, handles JSON-RPC, converts trace
│
├── aorchestra/runtime/pi_worker/
│   ├── package.json                 # Pi npm deps (pi-agent-core, pi-ai, typebox)
│   ├── tsconfig.json
│   ├── README.md                    # Operator notes
│   └── src/
│       ├── index.ts                 # Entrypoint: read JSON-RPC from stdin, write to stdout
│       ├── protocol.ts              # JSON-RPC types matching Python side
│       ├── agent.ts                 # Wraps pi-agent-core's Agent; builds tools from spec
│       └── tools.ts                 # Synthesizes AO-backed tools that round-trip to Python
│
└── tests/runtime/                   # New test directory inside AOrchestra
    ├── __init__.py
    ├── conftest.py                  # AORCHESTRA_ROOT path injection helper
    ├── test_base.py                 # SubAgentSpec / SubAgentRunResult / Registry contract
    ├── test_react_runtime.py        # ReActRuntime preserves prior behaviour
    └── test_pi_runtime.py           # PiRuntime: mocked Node worker over a pipe pair
```

### Modified (inside AOrchestra)

- `aorchestra/tools/delegate.py` — `DelegateTaskTool.__init__` accepts `runtime_registry` + `runtime_name`; `__call__` body's "create sub-agent → runner.run" block becomes `runtime.run(spec, env)`. Old `runner: Any` param kept for back-compat shim.
- `aorchestra/runners/gaia_runner.py:181` — passes `runtime_registry=default_registry()` + `runtime_name="react"` so existing GAIA behaviour is untouched.
- `aorchestra/runners/terminalbench_runner.py:179` — same.
- `aorchestra/runners/swebench_runner.py:183` + `:419` — same. (SWE-bench uses `SWEBenchSubAgent`, not `ReActAgent`; Task 4 covers the second runtime registration.)

### Modified (inside claw-eval) — exactly one file

- `src/claw_eval/harnesses/aorchestra/_runner.py:375-380` — `DelegateTaskTool(env=..., runner=..., models=..., benchmark_type="gaia")` becomes `DelegateTaskTool(env=..., runtime_registry=default_registry(), runtime_name=os.environ.get("CLAWEVAL_AORCHESTRA_RUNTIME", "pi"), models=..., benchmark_type="gaia")`.

### Created (inside claw-eval) — documentation only

- Update `docs/superpowers/specs/aorchestra_decision.md` (Task 14) — append a new section documenting the runtime split.
- Update `docs/progress.md` (Task 14) — append Phase 5 wave summary.

---

## Wave 5-A — SubAgentRuntime Interface + ReActRuntime Wrapper

Goal: lock the runtime seam in place without changing any execution behaviour. After Wave 5-A, GAIA / TerminalBench / SWE-bench / claw-eval all still run the existing ReActAgent code path, but through `runtime.run(spec, env)` instead of `runner.run(sub_agent, env)`.

### Task 1: `SubAgentSpec` / `SubAgentRunResult` dataclasses + `SubAgentRuntime` Protocol

**Files:**
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/base.py`
- Create: `/data2/ruanjianhao/AOrchestra/tests/runtime/__init__.py`
- Create: `/data2/ruanjianhao/AOrchestra/tests/runtime/conftest.py`
- Create: `/data2/ruanjianhao/AOrchestra/tests/runtime/test_base.py`

**Interfaces:**
- Consumes: nothing (this is the seam).
- Produces:
  - `SubAgentSpec` — frozen dataclass with fields `instruction: str`, `context: str`, `tools: list[str]`, `model: str`, `original_question: str = ""`, `benchmark_type: str = "terminalbench"`, `max_steps: int = 30`, `metadata: dict[str, Any] = field(default_factory=dict)`.
  - `SubAgentRunResult` — dataclass with fields `status: Literal["done", "partial", "error"]`, `done: bool`, `steps: int`, `finish_result: dict[str, Any] | None`, `trace: list[dict[str, Any]]`, `cost: float = 0.0`, `input_tokens: int = 0`, `output_tokens: int = 0`, `error: str | None = None`. `trace` items are serializable dicts matching `StepRecord` fields (observation/action/reward/raw_response/done/info).
  - `class SubAgentRuntime(Protocol)` — `async def run(self, spec: SubAgentSpec, env: Any) -> SubAgentRunResult: ...`
  - `class RuntimeRegistry` — `register(name: str, runtime: SubAgentRuntime) -> None`, `get(name: str) -> SubAgentRuntime` (raises `KeyError` with helpful message), `names() -> list[str]`.
  - `def default_registry() -> RuntimeRegistry` — module-level singleton accessor. Returns an empty registry initially; later tasks register `"react"`, `"pi"`, etc.

- [ ] **Step 1: Create the test scaffolding**

Create `/data2/ruanjianhao/AOrchestra/tests/runtime/__init__.py`:

```python
```

Create `/data2/ruanjianhao/AOrchestra/tests/runtime/conftest.py`:

```python
"""Shared fixtures for runtime tests.

These tests run inside the AOrchestra repo. Because AOrchestra is not a pip
package, we add its root to sys.path before any aorchestra.* import. The
AORCHESTRA_ROOT env var lets CI override the path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_AORCHESTRA_ROOT = Path(
    os.environ.get("AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra")
).resolve()

if str(_AORCHESTRA_ROOT) not in sys.path:
    sys.path.insert(0, str(_AORCHESTRA_ROOT))
```

- [ ] **Step 2: Write failing tests**

Create `/data2/ruanjianhao/AOrchestra/tests/runtime/test_base.py`:

```python
"""Contract tests for SubAgentSpec / SubAgentRunResult / SubAgentRuntime / Registry."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from aorchestra.runtime.base import (
    RuntimeRegistry,
    SubAgentRunResult,
    SubAgentRuntime,
    SubAgentSpec,
    default_registry,
)


# ---------------------------------------------------------------------------
# SubAgentSpec
# ---------------------------------------------------------------------------


def test_subagent_spec_has_required_and_default_fields():
    spec = SubAgentSpec(
        instruction="extract text",
        context="prior OCR returned partial result",
        tools=["ocr_extract_text", "finish"],
        model="claude-sonnet-4-5",
    )
    assert spec.instruction == "extract text"
    assert spec.tools == ["ocr_extract_text", "finish"]
    assert spec.original_question == ""
    assert spec.benchmark_type == "terminalbench"
    assert spec.max_steps == 30
    assert spec.metadata == {}


def test_subagent_spec_is_frozen():
    spec = SubAgentSpec(
        instruction="x", context="", tools=[], model="m",
    )
    with pytest.raises(FrozenInstanceError):
        spec.instruction = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SubAgentRunResult
# ---------------------------------------------------------------------------


def test_subagent_run_result_defaults():
    r = SubAgentRunResult(
        status="done", done=True, steps=3, finish_result={"answer": "36080"},
        trace=[],
    )
    assert r.cost == 0.0
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.error is None


def test_subagent_run_result_status_must_be_known():
    # Static typing enforces the Literal; runtime doesn't, but we document
    # the contract via a positive assertion.
    for s in ("done", "partial", "error"):
        SubAgentRunResult(status=s, done=False, steps=0, finish_result=None, trace=[])


# ---------------------------------------------------------------------------
# RuntimeRegistry
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """Minimal Protocol-compatible stand-in for tests."""

    def __init__(self, label: str) -> None:
        self.label = label

    async def run(self, spec, env):
        return SubAgentRunResult(
            status="done", done=True, steps=0,
            finish_result={"label": self.label}, trace=[],
        )


def test_runtime_registry_register_and_get():
    reg = RuntimeRegistry()
    rt = _FakeRuntime("a")
    reg.register("fake", rt)
    assert reg.get("fake") is rt
    assert "fake" in reg.names()


def test_runtime_registry_get_unknown_raises_keyerror_with_message():
    reg = RuntimeRegistry()
    reg.register("react", _FakeRuntime("r"))
    with pytest.raises(KeyError) as exc:
        reg.get("nonexistent")
    msg = str(exc.value)
    assert "nonexistent" in msg
    assert "react" in msg  # error message lists known names


def test_runtime_registry_register_replace_warns_or_overwrites():
    """Re-registering the same name must not silently merge. We choose: overwrite."""
    reg = RuntimeRegistry()
    a = _FakeRuntime("a")
    b = _FakeRuntime("b")
    reg.register("dup", a)
    reg.register("dup", b)
    assert reg.get("dup") is b


# ---------------------------------------------------------------------------
# default_registry singleton
# ---------------------------------------------------------------------------


def test_default_registry_is_shared_singleton():
    a = default_registry()
    b = default_registry()
    assert a is b


# ---------------------------------------------------------------------------
# Protocol satisfaction (smoke test — Protocol uses structural typing)
# ---------------------------------------------------------------------------


def test_protocol_satisfaction_via_duck_typing():
    rt: SubAgentRuntime = _FakeRuntime("p")  # type: ignore[assignment]
    spec = SubAgentSpec(instruction="", context="", tools=[], model="")
    result = asyncio.run(rt.run(spec, env=None))
    assert isinstance(result, SubAgentRunResult)
    assert result.finish_result == {"label": "p"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run from the AOrchestra repo root:

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_base.py -v
```

Expected: collection error or import error — `aorchestra.runtime.base` does not exist yet.

- [ ] **Step 4: Implement `aorchestra/runtime/base.py`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/base.py`:

```python
"""SubAgentRuntime contract — the seam between AOrchestra orchestration
and the actual sub-agent execution loop.

Background: AO's <I, C, T, M> four-tuple is the intermediate representation.
The runtime is the execution backend. Historically DelegateTaskTool created
a ReActAgent inline and called the Runner directly; that hardcoded loop
prevented us from plugging in alternative sub-agent runtimes (Pi, custom,
mock). This module defines the contract every runtime satisfies.

This file deliberately contains NO execution logic — just the data
shapes, the Protocol, and a tiny registry. ReActRuntime / PiRuntime live
in sibling modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class SubAgentSpec:
    """The <I, C, T, M> four-tuple plus execution metadata.

    Frozen because runtimes may be invoked concurrently against the same
    spec object; accidental mutation is a real footgun.

    Fields
    ------
    instruction : str
        I — the actionable subtask written by MainAgent.
    context : str
        C — prior findings, hints, summary fragments. May be empty.
    tools : list[str]
        T — names of tools the sub-agent is allowed to call. The runtime
        is responsible for surfacing each name to its execution backend.
    model : str
        M — the LLM the sub-agent should drive. Resolved via the shared
        ``LLMsConfig`` singleton at runtime time.
    original_question : str
        The full task that MainAgent was solving. Some sub-agent prompts
        want it for grounding even though the immediate instruction is the
        scoped subtask.
    benchmark_type : str
        ``"gaia"`` | ``"terminalbench"`` | ``"swebench"`` — picks the
        appropriate prompt template inside a runtime that supports
        multiple benchmark families.
    max_steps : int
        Hard step budget. The runtime MUST enforce this (not just put it
        in the prompt — see pitfall #2 in docs/aopi.md).
    metadata : dict[str, Any]
        Free-form bag for runtime-specific tweaks. Unused fields are
        ignored. Keep keys lowercase_snake.
    """

    instruction: str
    context: str
    tools: list[str]
    model: str

    original_question: str = ""
    benchmark_type: str = "terminalbench"
    max_steps: int = 30
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubAgentRunResult:
    """Standardised output every runtime returns.

    ``trace`` items are dicts shaped like ``benchmark.common.runner.StepRecord``
    fields (observation/action/reward/raw_response/done/info). Runtimes that
    have a richer native event stream (e.g. Pi's tool_execution_start /
    tool_execution_end) MUST convert it to this shape before returning.
    Downstream consumers (claw-eval trace adapter, AO trace formatters,
    DelegateTaskTool._summarize_trace) only know about this shape.
    """

    status: Literal["done", "partial", "error"]
    done: bool
    steps: int

    finish_result: dict[str, Any] | None
    trace: list[dict[str, Any]]

    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


class SubAgentRuntime(Protocol):
    """Structural contract for sub-agent execution backends.

    Runtimes are stateless across runs; each ``run()`` call is independent.
    Concurrent ``run()`` invocations against the same runtime instance must
    be safe (the registry hands out a single instance per name).
    """

    async def run(
        self,
        spec: SubAgentSpec,
        env: Any,
    ) -> SubAgentRunResult:
        ...


class RuntimeRegistry:
    """Name → SubAgentRuntime mapping.

    Re-registering the same name overwrites the prior entry. We chose
    overwrite (rather than raise) because tests and configuration overrides
    routinely need to swap implementations.
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, SubAgentRuntime] = {}

    def register(self, name: str, runtime: SubAgentRuntime) -> None:
        self._runtimes[name] = runtime

    def get(self, name: str) -> SubAgentRuntime:
        if name not in self._runtimes:
            known = sorted(self._runtimes)
            raise KeyError(
                f"Unknown SubAgentRuntime: {name!r}. Registered: {known}"
            )
        return self._runtimes[name]

    def names(self) -> list[str]:
        return list(self._runtimes)


_DEFAULT_REGISTRY: RuntimeRegistry | None = None


def default_registry() -> RuntimeRegistry:
    """Process-wide singleton. Created on first access."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = RuntimeRegistry()
    return _DEFAULT_REGISTRY
```

- [ ] **Step 5: Create the package `__init__.py`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`:

```python
"""Pluggable SubAgent runtime backends.

Phase 5 — see /data2/ruanjianhao/claw-eval/docs/superpowers/plans/2026-06-25-aorchestra-pi-runtime.md
and /data2/ruanjianhao/claw-eval/docs/aopi.md.
"""
from __future__ import annotations

from aorchestra.runtime.base import (
    RuntimeRegistry,
    SubAgentRunResult,
    SubAgentRuntime,
    SubAgentSpec,
    default_registry,
)

__all__ = [
    "RuntimeRegistry",
    "SubAgentRunResult",
    "SubAgentRuntime",
    "SubAgentSpec",
    "default_registry",
]
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_base.py -v
```

Expected: 8 passed.

- [ ] **Step 7: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
git add aorchestra/runtime/__init__.py aorchestra/runtime/base.py \
        tests/runtime/__init__.py tests/runtime/conftest.py tests/runtime/test_base.py
git commit -m "feat(runtime): SubAgentRuntime Protocol + dataclasses + registry"
```

### Task 2: `ReActRuntime` wrapper

**Files:**
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/react_runtime.py`
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`
- Create: `/data2/ruanjianhao/AOrchestra/tests/runtime/test_react_runtime.py`

**Interfaces:**
- Consumes: `SubAgentSpec`, `SubAgentRunResult` from Task 1. From upstream AOrchestra: `aorchestra.subagents.ReActAgent`, `aorchestra.subagents.SWEBenchSubAgent`, `base.agent.memory.Memory`, `base.engine.async_llm.LLMsConfig`, `base.engine.async_llm.create_llm_instance`, `benchmark.common.runner.Runner`, `benchmark.common.runner.StepRecord`.
- Produces:
  - `class ReActRuntime` — concrete `SubAgentRuntime`. Constructor takes no required args (it instantiates an internal `Runner()`). The `run(spec, env)` method:
    1. Looks up the LLM for `spec.model` via `LLMsConfig.default().get(spec.model)` and wraps it with `create_llm_instance`.
    2. Captures `env.instruction` (may be `None`).
    3. Sets `env.instruction = spec.instruction` (sub-agent perspective).
    4. Creates an inner `ReActAgent` (or `SWEBenchSubAgent` when `spec.benchmark_type == "swebench"`) with the same constructor args used today by `DelegateTaskTool` lines 137-157.
    5. Runs `await self._runner.run(sub_agent, env)` where `self._runner = Runner()`.
    6. Extracts `finish_result` from `result.trace[-1].info` exactly like `DelegateTaskTool.__call__` lines 168-173.
    7. Converts each `StepRecord` in `result.trace` into a dict (using the `_make_serializable` helper from `delegate.py`; either import it or copy it locally).
    8. Determines `status`: `"done"` if `result.done` and `finish_result is not None`, else `"partial"`.
    9. Restores `env.instruction` to the captured prior value in a `finally`.
    10. Returns `SubAgentRunResult(status=..., done=result.done, steps=result.steps, finish_result=finish_result, trace=trace_serializable, cost=result.cost, input_tokens=result.input_tokens, output_tokens=result.output_tokens, error=None)`.
  - On exceptions: catch, restore `env.instruction`, return `SubAgentRunResult(status="error", done=False, steps=0, finish_result=None, trace=[], error=str(exc))`.

- [ ] **Step 1: Write failing tests**

Create `/data2/ruanjianhao/AOrchestra/tests/runtime/test_react_runtime.py`:

```python
"""ReActRuntime contract test — preserves the current DelegateTaskTool behaviour.

This test does NOT depend on a real LLM or environment. Instead we monkey-patch
``Runner.run`` (the workhorse) to return a synthetic ``LevelResult`` and assert
that ReActRuntime translates it to the standard SubAgentRunResult shape.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from aorchestra.runtime.base import SubAgentSpec
from aorchestra.runtime.react_runtime import ReActRuntime
from benchmark.common.runner import LevelResult, StepRecord


class _FakeEnv:
    """Bare-bones Environment-shaped stub. ReActRuntime only reads/writes
    ``instruction`` and never calls reset/step (Runner does that, and we
    monkey-patch Runner.run away)."""

    def __init__(self) -> None:
        self.instruction = "original-task"


@pytest.fixture
def fake_step_record():
    return StepRecord(
        observation={"text": "ocr returned"},
        action={"action": "finish", "params": {"status": "done", "answer": "36080"}},
        reward=1.0,
        raw_response='{"action": "finish", ...}',
        done=True,
        info={"finished": True, "finish_result": {"status": "done", "answer": "36080"}},
    )


@pytest.fixture
def fake_level_result(fake_step_record):
    return LevelResult(
        model="claude-sonnet-4-5",
        total_reward=1.0,
        steps=1,
        done=True,
        trace=[fake_step_record],
        cost=0.04,
        input_tokens=512,
        output_tokens=64,
    )


@pytest.fixture
def patched_llm(monkeypatch):
    """Avoid hitting a real LLM by stubbing create_llm_instance + LLMsConfig."""
    fake_llm = SimpleNamespace(model="claude-sonnet-4-5")

    from base.engine import async_llm as al

    monkeypatch.setattr(
        al,
        "create_llm_instance",
        lambda cfg: fake_llm,
    )
    monkeypatch.setattr(
        al.LLMsConfig,
        "default",
        classmethod(lambda cls: SimpleNamespace(get=lambda name: object())),
    )
    return fake_llm


def test_react_runtime_returns_done_when_finish_result_present(
    monkeypatch, patched_llm, fake_level_result,
):
    async def fake_run(self, agent, env):  # noqa: ARG001
        return fake_level_result

    monkeypatch.setattr(
        "benchmark.common.runner.Runner.run", fake_run, raising=True,
    )

    rt = ReActRuntime()
    env = _FakeEnv()
    spec = SubAgentSpec(
        instruction="extract budget text",
        context="prior OCR partial",
        tools=["ocr_extract_text", "finish"],
        model="claude-sonnet-4-5",
        benchmark_type="gaia",
        max_steps=30,
    )

    result = asyncio.run(rt.run(spec, env))

    assert result.status == "done"
    assert result.done is True
    assert result.steps == 1
    assert result.finish_result == {"status": "done", "answer": "36080"}
    assert len(result.trace) == 1
    # Trace item is a dict, not a dataclass
    assert isinstance(result.trace[0], dict)
    assert result.trace[0]["reward"] == 1.0
    assert result.cost == 0.04
    assert result.input_tokens == 512
    assert result.output_tokens == 64
    assert result.error is None


def test_react_runtime_restores_env_instruction(
    monkeypatch, patched_llm, fake_level_result,
):
    async def fake_run(self, agent, env):  # noqa: ARG001
        # During the run, env.instruction should have been overwritten.
        assert env.instruction == "delegated-subtask"
        return fake_level_result

    monkeypatch.setattr(
        "benchmark.common.runner.Runner.run", fake_run, raising=True,
    )

    rt = ReActRuntime()
    env = _FakeEnv()
    assert env.instruction == "original-task"
    spec = SubAgentSpec(
        instruction="delegated-subtask",
        context="", tools=["finish"], model="claude-sonnet-4-5",
    )
    asyncio.run(rt.run(spec, env))
    # Restored
    assert env.instruction == "original-task"


def test_react_runtime_returns_partial_when_done_but_no_finish_result(
    monkeypatch, patched_llm,
):
    """A timeout-style end (done=True from max_steps) leaves no finish_result."""
    no_finish_record = StepRecord(
        observation={}, action={"action": "noop"}, reward=0.0,
        raw_response="", done=True, info={},
    )
    no_finish_result = LevelResult(
        model="claude-sonnet-4-5", total_reward=0.0, steps=1, done=True,
        trace=[no_finish_record], cost=0.01,
    )

    async def fake_run(self, agent, env):  # noqa: ARG001
        return no_finish_result

    monkeypatch.setattr(
        "benchmark.common.runner.Runner.run", fake_run, raising=True,
    )

    rt = ReActRuntime()
    spec = SubAgentSpec(
        instruction="x", context="", tools=["finish"], model="claude-sonnet-4-5",
    )
    result = asyncio.run(rt.run(spec, _FakeEnv()))
    assert result.status == "partial"
    assert result.finish_result is None


def test_react_runtime_catches_exceptions(monkeypatch, patched_llm):
    async def fake_run(self, agent, env):  # noqa: ARG001
        raise RuntimeError("simulated downstream crash")

    monkeypatch.setattr(
        "benchmark.common.runner.Runner.run", fake_run, raising=True,
    )

    rt = ReActRuntime()
    env = _FakeEnv()
    spec = SubAgentSpec(
        instruction="x", context="", tools=["finish"], model="claude-sonnet-4-5",
    )
    result = asyncio.run(rt.run(spec, env))
    assert result.status == "error"
    assert result.done is False
    assert result.steps == 0
    assert result.trace == []
    assert result.error and "simulated" in result.error
    # env.instruction restored even on error
    assert env.instruction == "original-task"


def test_react_runtime_swebench_uses_dedicated_subagent(
    monkeypatch, patched_llm, fake_level_result,
):
    """When benchmark_type='swebench' the runtime constructs SWEBenchSubAgent,
    not ReActAgent. We assert this by inspecting the agent type the inner
    runner saw."""
    seen_agent_type = []

    async def fake_run(self, agent, env):  # noqa: ARG001
        seen_agent_type.append(type(agent).__name__)
        return fake_level_result

    monkeypatch.setattr(
        "benchmark.common.runner.Runner.run", fake_run, raising=True,
    )

    rt = ReActRuntime()
    spec = SubAgentSpec(
        instruction="x", context="", tools=["finish"],
        model="claude-sonnet-4-5", benchmark_type="swebench",
    )
    asyncio.run(rt.run(spec, _FakeEnv()))
    assert seen_agent_type == ["SWEBenchSubAgent"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_react_runtime.py -v
```

Expected: `ModuleNotFoundError: aorchestra.runtime.react_runtime`.

- [ ] **Step 3: Implement `react_runtime.py`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/react_runtime.py`:

```python
"""ReActRuntime — preserves the historical DelegateTaskTool dispatch path.

Before Phase 5, DelegateTaskTool.__call__ created a ReActAgent / SWEBenchSubAgent
inline and called Runner.run(sub_agent, env). This runtime is a faithful
extraction of that block, exposed as the ``SubAgentRuntime`` Protocol.

No behavioural change is intended. Tests assert the StepRecord-to-dict
conversion, env.instruction save/restore, and exception swallowing match the
pre-refactor behaviour.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from base.agent.memory import Memory
from base.engine.async_llm import LLMsConfig, create_llm_instance
from base.engine.logs import logger
from benchmark.common.runner import Runner

from aorchestra.runtime.base import SubAgentRunResult, SubAgentSpec


def _make_serializable(obj: Any) -> Any:
    """Mirror of aorchestra.tools.delegate._make_serializable.

    Recursively converts dataclasses / dicts / sequences into JSON-friendly
    forms. Falls back to ``str(obj)`` for unknown types.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _make_serializable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_serializable(item) for item in obj]
    return str(obj)


class ReActRuntime:
    """Concrete ``SubAgentRuntime`` that drives the existing ReActAgent loop.

    Creates one ``Runner`` instance lazily and reuses it across runs (Runner
    is stateless between calls).
    """

    def __init__(self) -> None:
        self._runner = Runner()

    async def run(self, spec: SubAgentSpec, env: Any) -> SubAgentRunResult:
        original_instruction = getattr(env, "instruction", None)
        try:
            llm = create_llm_instance(LLMsConfig.default().get(spec.model))

            if spec.benchmark_type == "swebench":
                from aorchestra.subagents import SWEBenchSubAgent

                sub_agent = SWEBenchSubAgent(
                    llm=llm,
                    task_instruction=spec.instruction,
                    context=spec.context,
                    original_question=spec.original_question,
                    memory=Memory(llm=llm, max_memory=20),
                )
            else:
                from aorchestra.subagents import ReActAgent

                sub_agent = ReActAgent(
                    llm=llm,
                    benchmark_type=spec.benchmark_type,
                    task_instruction=spec.instruction,
                    context=spec.context,
                    original_question=spec.original_question,
                    allowed_tools=spec.tools or None,
                    memory=Memory(llm=llm, max_memory=10),
                )

            # Switch env's perceived instruction for the sub-agent's perspective.
            if hasattr(env, "instruction"):
                env.instruction = spec.instruction

            result = await self._runner.run(sub_agent, env)

            finish_result = None
            if result.trace:
                last = result.trace[-1]
                if last.info.get("finished") and last.info.get("finish_result"):
                    finish_result = last.info["finish_result"]

            status = "done" if (result.done and finish_result is not None) else "partial"
            trace_dicts = [_make_serializable(s) for s in (result.trace or [])]

            return SubAgentRunResult(
                status=status,
                done=result.done,
                steps=result.steps,
                finish_result=finish_result,
                trace=trace_dicts,
                cost=result.cost,
                input_tokens=getattr(result, "input_tokens", 0) or 0,
                output_tokens=getattr(result, "output_tokens", 0) or 0,
                error=None,
            )

        except Exception as exc:  # noqa: BLE001 — runtimes never raise
            logger.error(f"[ReActRuntime] error: {exc}")
            return SubAgentRunResult(
                status="error",
                done=False,
                steps=0,
                finish_result=None,
                trace=[],
                cost=0.0,
                input_tokens=0,
                output_tokens=0,
                error=str(exc),
            )

        finally:
            if hasattr(env, "instruction"):
                env.instruction = original_instruction
```

- [ ] **Step 4: Export from package**

Update `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`:

```python
"""Pluggable SubAgent runtime backends.

Phase 5 — see /data2/ruanjianhao/claw-eval/docs/superpowers/plans/2026-06-25-aorchestra-pi-runtime.md
and /data2/ruanjianhao/claw-eval/docs/aopi.md.
"""
from __future__ import annotations

from aorchestra.runtime.base import (
    RuntimeRegistry,
    SubAgentRunResult,
    SubAgentRuntime,
    SubAgentSpec,
    default_registry,
)
from aorchestra.runtime.react_runtime import ReActRuntime

__all__ = [
    "ReActRuntime",
    "RuntimeRegistry",
    "SubAgentRunResult",
    "SubAgentRuntime",
    "SubAgentSpec",
    "default_registry",
]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/ -v
```

Expected: 13 passed (8 from Task 1 + 5 here).

- [ ] **Step 6: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
git add aorchestra/runtime/react_runtime.py aorchestra/runtime/__init__.py \
        tests/runtime/test_react_runtime.py
git commit -m "feat(runtime): ReActRuntime preserves DelegateTaskTool's prior behaviour"
```

### Task 3: Register `"react"` runtime in `default_registry()`

**Files:**
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`
- Modify: `/data2/ruanjianhao/AOrchestra/tests/runtime/test_base.py`

**Interfaces:**
- Consumes: `ReActRuntime` (Task 2), `default_registry()` (Task 1).
- Produces: First call to `default_registry()` now returns a registry containing `"react" → ReActRuntime()`.

- [ ] **Step 1: Add the registration assertion to test_base.py**

Append to `/data2/ruanjianhao/AOrchestra/tests/runtime/test_base.py`:

```python


# ---------------------------------------------------------------------------
# Default registry pre-registration
# ---------------------------------------------------------------------------


def test_default_registry_has_react_preregistered():
    from aorchestra.runtime import ReActRuntime

    reg = default_registry()
    assert "react" in reg.names()
    rt = reg.get("react")
    assert isinstance(rt, ReActRuntime)
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_base.py::test_default_registry_has_react_preregistered -v
```

Expected: FAIL with `AssertionError: assert 'react' in []`.

- [ ] **Step 3: Update `aorchestra/runtime/__init__.py` to auto-register**

Update `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`:

```python
"""Pluggable SubAgent runtime backends.

Phase 5 — see /data2/ruanjianhao/claw-eval/docs/superpowers/plans/2026-06-25-aorchestra-pi-runtime.md
and /data2/ruanjianhao/claw-eval/docs/aopi.md.

On import this module ensures the ``default_registry()`` has ``"react"`` pre-
registered. ``"pi"`` is registered lazily by ``aorchestra.runtime.pi_runtime``
on its first import to avoid forcing a Node dependency on react-only callers.
"""
from __future__ import annotations

from aorchestra.runtime.base import (
    RuntimeRegistry,
    SubAgentRunResult,
    SubAgentRuntime,
    SubAgentSpec,
    default_registry,
)
from aorchestra.runtime.react_runtime import ReActRuntime


def _register_defaults() -> None:
    reg = default_registry()
    if "react" not in reg.names():
        reg.register("react", ReActRuntime())


_register_defaults()


__all__ = [
    "ReActRuntime",
    "RuntimeRegistry",
    "SubAgentRunResult",
    "SubAgentRuntime",
    "SubAgentSpec",
    "default_registry",
]
```

- [ ] **Step 4: Run all runtime tests**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/ -v
```

Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
git add aorchestra/runtime/__init__.py tests/runtime/test_base.py
git commit -m "feat(runtime): pre-register ReActRuntime as 'react' on import"
```

### Task 4: Wire `DelegateTaskTool` through the runtime registry

**Files:**
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/tools/delegate.py`
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/runners/gaia_runner.py`
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/runners/terminalbench_runner.py`
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/runners/swebench_runner.py`
- Create: `/data2/ruanjianhao/AOrchestra/tests/runtime/test_delegate_uses_runtime.py`

**Interfaces:**
- Consumes: `SubAgentSpec`, `default_registry()` from Tasks 1 / 3.
- Produces:
  - `DelegateTaskTool.__init__` now accepts two new optional keyword arguments:
    - `runtime_registry: RuntimeRegistry | None = None` — defaults to `default_registry()` if `None`.
    - `runtime_name: str = "react"` — the registered runtime to use.
  - Back-compat: the old `runner` positional arg is still accepted (and stored at `self.runner`) but ignored when `runtime_registry` is provided. This means the three existing runner files (gaia/terminalbench/swebench) keep passing `runner=...` and continue to work unchanged.
  - `DelegateTaskTool.__call__` no longer creates a sub-agent or calls `runner.run()`. Instead it builds a `SubAgentSpec` from the call's args and delegates to `self._runtime.run(spec, self.env)`. The `_summarize_trace`, env.instruction restore, and result dict assembly stay in `DelegateTaskTool`.

- [ ] **Step 1: Write failing test**

Create `/data2/ruanjianhao/AOrchestra/tests/runtime/test_delegate_uses_runtime.py`:

```python
"""DelegateTaskTool routes execution through the runtime registry.

This test does NOT verify ReActRuntime behaviour (that's test_react_runtime).
It verifies the *seam*: DelegateTaskTool builds a SubAgentSpec and calls
runtime.run(spec, env), then assembles its return dict from the result.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aorchestra.runtime.base import (
    RuntimeRegistry,
    SubAgentRunResult,
    SubAgentSpec,
)
from aorchestra.tools.delegate import DelegateTaskTool


class _FakeEnv:
    def __init__(self) -> None:
        self.instruction = "original-task"


class _CapturingRuntime:
    """Records the spec/env it was called with and returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[tuple[SubAgentSpec, object]] = []
        self.next_result = SubAgentRunResult(
            status="done", done=True, steps=2,
            finish_result={"status": "done", "answer": "36080"},
            trace=[
                {
                    "observation": {"text": "ocr"},
                    "action": {"action": "finish", "params": {}},
                    "reward": 1.0,
                    "raw_response": "{}",
                    "done": True,
                    "info": {},
                },
            ],
            cost=0.05, input_tokens=128, output_tokens=16,
        )

    async def run(self, spec, env):  # noqa: D401
        self.calls.append((spec, env))
        return self.next_result


@pytest.fixture
def patched_summarize(monkeypatch):
    """Avoid hitting an LLM during _summarize_trace."""
    async def fake(self, trace, task_instruction):  # noqa: ARG001
        return "stub summary"

    monkeypatch.setattr(DelegateTaskTool, "_summarize_trace", fake)


def test_delegate_uses_named_runtime(patched_summarize):
    reg = RuntimeRegistry()
    rt = _CapturingRuntime()
    reg.register("capture", rt)

    env = _FakeEnv()
    tool = DelegateTaskTool(
        env=env,
        runner=None,  # back-compat: explicitly None
        models=["claude-sonnet-4-5"],
        benchmark_type="gaia",
        runtime_registry=reg,
        runtime_name="capture",
    )

    out = asyncio.run(tool(
        task_instruction="extract budget text",
        model="claude-sonnet-4-5",
        context="prior OCR partial",
        tools=["ocr_extract_text", "finish"],
    ))

    # Runtime was invoked once with the right spec
    assert len(rt.calls) == 1
    spec, called_env = rt.calls[0]
    assert called_env is env
    assert isinstance(spec, SubAgentSpec)
    assert spec.instruction == "extract budget text"
    assert spec.context == "prior OCR partial"
    assert spec.tools == ["ocr_extract_text", "finish"]
    assert spec.model == "claude-sonnet-4-5"
    assert spec.benchmark_type == "gaia"
    assert spec.original_question == "original-task"
    assert spec.max_steps > 0  # populated from the env's max_steps

    # Return dict matches the historical shape DelegateTaskTool exposed
    assert out["model"] == "claude-sonnet-4-5"
    assert out["steps_taken"] == 2
    assert out["done"] is True
    assert out["cost"] == 0.05
    assert out["finish_result"] == {"status": "done", "answer": "36080"}
    assert isinstance(out["trace"], list) and len(out["trace"]) == 1
    assert out["trace_summary"] == "stub summary"
    # New field exposed for observability
    assert out["runtime"] == "capture"


def test_delegate_defaults_to_react_when_no_registry_passed(monkeypatch):
    """Back-compat: existing callers that pass `runner=...` and no runtime_*
    args still work — they get the default_registry's ``"react"`` entry."""
    seen = []

    class _SpyRuntime:
        async def run(self, spec, env):  # noqa: ARG001
            seen.append("called")
            return SubAgentRunResult(
                status="done", done=True, steps=1, finish_result={},
                trace=[],
            )

    # Replace the default registry's react entry with a spy
    from aorchestra.runtime import default_registry

    reg = default_registry()
    original_react = reg.get("react")
    reg.register("react", _SpyRuntime())
    try:
        # Stub summarizer
        async def fake_summarize(self, trace, task_instruction):  # noqa: ARG001
            return "stub"

        monkeypatch.setattr(DelegateTaskTool, "_summarize_trace", fake_summarize)

        env = _FakeEnv()
        tool = DelegateTaskTool(
            env=env,
            runner=None,
            models=["claude-sonnet-4-5"],
            benchmark_type="terminalbench",
            # No runtime_* args → default to ("default_registry()", "react")
        )
        asyncio.run(tool(
            task_instruction="x", model="claude-sonnet-4-5",
            context="", tools=[],
        ))
        assert seen == ["called"]
    finally:
        reg.register("react", original_react)


def test_delegate_unknown_runtime_returns_error_dict(monkeypatch):
    """If runtime_name doesn't exist, the tool surfaces a structured error
    (it must NEVER raise out of __call__)."""
    async def fake_summarize(self, trace, task_instruction):  # noqa: ARG001
        return "stub"

    monkeypatch.setattr(DelegateTaskTool, "_summarize_trace", fake_summarize)

    env = _FakeEnv()
    tool = DelegateTaskTool(
        env=env, runner=None, models=["m"], benchmark_type="gaia",
        runtime_registry=RuntimeRegistry(),
        runtime_name="missing",
    )
    out = asyncio.run(tool(
        task_instruction="x", model="m", context="", tools=[],
    ))
    assert out.get("error", "").startswith("Unknown SubAgentRuntime")
    assert out["done"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_delegate_uses_runtime.py -v
```

Expected: import error or `TypeError: __init__() got an unexpected keyword argument 'runtime_registry'`.

- [ ] **Step 3: Refactor `DelegateTaskTool`**

Edit `/data2/ruanjianhao/AOrchestra/aorchestra/tools/delegate.py`:

Replace the entire `__init__` (lines 66-104) with the version below. Also replace the body of `__call__` (lines 106-204) with the version below. The `_summarize_trace` method (lines 206-274) stays unchanged. The top-level `_make_serializable` helper (lines 21-32) stays.

```python
    def __init__(
        self,
        env,
        runner=None,
        models: list = None,
        benchmark_type: str = "terminalbench",
        alias_to_model: Dict[str, str] = None,
        runtime_registry=None,
        runtime_name: str = "react",
    ):
        """Construct the delegation tool.

        Parameters
        ----------
        env :
            The current Environment instance (forwarded to the runtime).
        runner :
            Legacy parameter retained for back-compat with callers that
            pass it positionally. **Ignored** when ``runtime_registry`` is
            supplied (which is the default). When you really want to use a
            custom Runner directly, instantiate a custom runtime instead.
        models :
            List of allowed sub-agent model names (used to validate the
            ``model`` arg the LLM picks and to seed alias display).
        benchmark_type :
            ``"gaia"`` | ``"terminalbench"`` | ``"swebench"``.
        alias_to_model :
            Optional display-name → real-model mapping for prompt masking.
        runtime_registry :
            Source of sub-agent runtimes. Defaults to
            ``aorchestra.runtime.default_registry()``.
        runtime_name :
            Which runtime to ask the registry for. Defaults to ``"react"``,
            preserving prior behaviour.
        """
        super().__init__()
        self.env = env
        self.runner = runner  # legacy; kept for callers that read it
        self.models = models or []
        self.benchmark_type = benchmark_type
        self.alias_to_model = alias_to_model or {}

        # Bind a runtime up-front. We do not look it up per call so changes
        # to the registry mid-run don't surprise the orchestration.
        if runtime_registry is None:
            from aorchestra.runtime import default_registry

            runtime_registry = default_registry()
        self._runtime_registry = runtime_registry
        self._runtime_name = runtime_name

        # Create corresponding trace formatter
        if benchmark_type == "gaia":
            self._trace_formatter = create_gaia_formatter()
        elif benchmark_type == "swebench":
            self._trace_formatter = create_swebench_formatter()
        else:
            self._trace_formatter = create_terminalbench_formatter()

        # Set model enum (using alias or real name)
        display_models = list(self.alias_to_model.keys()) if self.alias_to_model else self.models
        self.parameters = {
            "type": "object",
            "properties": {
                "task_instruction": {"type": "string", "description": "Task for SubAgent"},
                "context": {"type": "string", "description": "Additional context/hints"},
                "model": {
                    "type": "string",
                    "description": f"Model to use. MUST be one of: {display_models}",
                    "enum": display_models,
                },
                "tools": {"type": "array", "items": {"type": "string"}, "description": "Tools for SubAgent (optional)"},
            },
            "required": ["task_instruction", "model"],
        }

    async def __call__(
        self,
        task_instruction: str,
        model: str,
        context: str = "",
        tools: List[str] = None,
    ) -> Dict:
        """Execute the delegated task via the bound SubAgentRuntime."""
        from aorchestra.runtime import SubAgentSpec

        # 1. Resolve model alias and validate.
        real_model = self.alias_to_model.get(model, model)
        if self.models and real_model not in self.models:
            return {"error": f"Invalid model: {model}", "steps_taken": 0, "done": False}

        # 2. Compose the spec.
        original_question = getattr(self.env, "instruction", "") or ""
        max_steps = 30
        try:
            max_steps = self.env.get_basic_info().max_steps  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        spec = SubAgentSpec(
            instruction=task_instruction,
            context=context or "",
            tools=list(tools or []),
            model=real_model,
            original_question=original_question,
            benchmark_type=self.benchmark_type,
            max_steps=int(max_steps),
        )

        # 3. Resolve the runtime. Surface a clean error if the name is unknown
        # — runtimes themselves never raise.
        try:
            runtime = self._runtime_registry.get(self._runtime_name)
        except KeyError as exc:
            return {
                "error": str(exc),
                "steps_taken": 0,
                "done": False,
                "cost": 0.0,
                "runtime": self._runtime_name,
            }

        logger.info(
            f"[DelegateTool] runtime={self._runtime_name} model={real_model} "
            f"tools={spec.tools}"
        )

        # 4. Run.
        result = await runtime.run(spec, self.env)

        # 5. Summarize trace (uses self.models[0] — see _summarize_trace).
        trace_summary = await self._summarize_trace(result.trace, task_instruction)

        # 6. Build the return dict in the historical shape consumed by
        # MainAgent + claw-eval _runner.py + _trace_adapter.
        return {
            "runtime": self._runtime_name,
            "model": real_model,
            "tools_assigned": tools,
            "steps_taken": result.steps,
            "done": result.done,
            "cost": result.cost,
            "finish_result": result.finish_result,
            "trace": result.trace,
            "trace_summary": trace_summary,
            "statistics": {
                "total_steps": result.steps,
                "max_steps": spec.max_steps,
                "completed": result.done,
            },
            "error": result.error,
        }
```

- [ ] **Step 4: Update runners to pass `runner` as a keyword argument**

The three runner files still pass `runner=...` positionally in some places. Update each to ensure they call `DelegateTaskTool(env=..., runner=..., models=..., benchmark_type=...)` with explicit keyword args. This is defensive — it future-proofs against the legacy positional `runner` argument being removed someday.

Edit `/data2/ruanjianhao/AOrchestra/aorchestra/runners/gaia_runner.py` around line 181. Find the `delegate_tool = DelegateTaskTool(...)` block and ensure every argument is passed by keyword:

```python
delegate_tool = DelegateTaskTool(
    env=env,
    runner=runner,
    models=self.sub_models,
    benchmark_type="gaia",
    alias_to_model=alias_to_model,
)
```

Do the same in `aorchestra/runners/terminalbench_runner.py` (search for `DelegateTaskTool(` — around line 175) and `aorchestra/runners/swebench_runner.py` (two call sites — around lines 179 and 415).

- [ ] **Step 5: Run the delegate test**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_delegate_uses_runtime.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the entire runtime test suite plus a smoke check**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/ -v
```

Expected: 17 passed.

```bash
cd /data2/ruanjianhao/AOrchestra
python -c "from aorchestra.tools.delegate import DelegateTaskTool; print('import ok')"
```

Expected: `import ok`.

- [ ] **Step 7: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
git add aorchestra/tools/delegate.py aorchestra/runners/ tests/runtime/test_delegate_uses_runtime.py
git commit -m "refactor(delegate): route execution through SubAgentRuntime registry"
```

---

## Wave 5-B — Node Pi Worker (TypeScript)

Goal: a Node subprocess that, when spawned with a JSON-RPC request on stdin, drives a Pi `Agent` to completion using AO-supplied tools, and writes per-step events plus a final result to stdout.

### Task 5: Pi worker package scaffolding + JSON-RPC protocol types

**Files:**
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/package.json`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/tsconfig.json`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/README.md`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/protocol.ts`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/.gitignore`

**Interfaces:**
- Consumes: nothing yet (this is scaffolding).
- Produces:
  - Compiled output goes to `dist/`. Node 22+ is the runtime.
  - `protocol.ts` exports TypeScript types that **exactly mirror** the Python types used in Task 7 (`PiRuntime`). Names: `RunStart`, `ToolCall`, `ToolResult`, `RunEnd`, `LogEvent`, `RpcRequest`, `RpcResponse`.

- [ ] **Step 1: Create `package.json`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/package.json`:

```json
{
  "name": "aorchestra-pi-worker",
  "version": "0.1.0",
  "description": "Node-side Pi agent worker for AOrchestra PiRuntime",
  "private": true,
  "type": "module",
  "main": "dist/index.js",
  "scripts": {
    "build": "tsc -p tsconfig.json",
    "start": "node dist/index.js",
    "typecheck": "tsc -p tsconfig.json --noEmit"
  },
  "engines": {
    "node": ">=22"
  },
  "dependencies": {
    "@earendil-works/pi-agent-core": "0.80.2",
    "@earendil-works/pi-ai": "0.80.2",
    "typebox": "^1.1.38"
  },
  "devDependencies": {
    "typescript": "^5.9.0",
    "@types/node": "^22.0.0"
  }
}
```

- [ ] **Step 2: Create `tsconfig.json`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "outDir": "dist",
    "rootDir": "src",
    "declaration": false,
    "noEmitOnError": true
  },
  "include": ["src/**/*.ts"]
}
```

- [ ] **Step 3: Create `.gitignore`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/.gitignore`:

```
node_modules
dist
*.log
```

- [ ] **Step 4: Create `README.md`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/README.md`:

```markdown
# aorchestra-pi-worker

Node-side worker that drives a Pi `Agent` for AOrchestra's `PiRuntime`.

## Build

```
npm install
npm run build
```

## Protocol

Each invocation reads JSON-RPC 2.0 messages from stdin (one per line) and
writes responses to stdout. stderr is for human logs only — never JSON.

Message types (see `src/protocol.ts`):

- `run_start` (Python → Node) — kicks off a run with a `SubAgentSpec` plus
  the list of tool descriptors the Python side will service.
- `tool_call` (Node → Python) — Pi asked to invoke a tool. Python performs
  `Environment.step(action)` and returns the observation as a `tool_result`.
- `tool_result` (Python → Node) — the result the tool was waiting for.
- `log` (Node → Python) — informational; surfaced via Python logging.
- `run_end` (Node → Python) — final result with full trace + usage.

All Pi `Agent` runs use:

- `noTools: "builtin"` — Pi's built-in `bash` / `read` / `edit` / `write`
  tools are disabled (see pitfall #3 in `docs/aopi.md`).
- `toolExecution: "sequential"` — AO Environment is stateful; concurrent
  steps would corrupt it (pitfall #4).
- Step budget enforced by the Python tool gateway — if Python returns
  `{done: true, termination_reason: "max_steps"}`, the worker finishes the
  current turn and exits (pitfall #2).
```

- [ ] **Step 5: Create `protocol.ts`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/protocol.ts`:

```typescript
/**
 * JSON-RPC 2.0 message types for the AOrchestra ↔ Pi Worker bridge.
 *
 * These types MUST stay in lockstep with the Python definitions in
 * aorchestra/runtime/pi_runtime.py. If you add a field, add it on both sides.
 *
 * Wire format: one JSON object per line on stdin / stdout. stderr is for
 * humans only — never a JSON payload.
 */

export type Json =
  | string
  | number
  | boolean
  | null
  | { [k: string]: Json }
  | Json[];

/**
 * Sent Python → Node to start a run. The Node worker is expected to:
 * 1. Construct a Pi Agent with the given system prompt, model, tools.
 * 2. Drive the agent loop end-to-end.
 * 3. Emit `tool_call` for each tool invocation (await `tool_result`).
 * 4. Emit `run_end` once done.
 */
export interface RunStart {
  type: "run_start";
  run_id: string;
  spec: {
    instruction: string;
    context: string;
    tools: string[]; // names — full descriptors in `tool_descriptors`
    model: string;
    original_question: string;
    benchmark_type: string;
    max_steps: number;
    metadata: { [k: string]: Json };
  };
  tool_descriptors: ToolDescriptor[];
  llm_endpoint: {
    base_url: string;
    api_key: string;
  };
}

export interface ToolDescriptor {
  name: string;
  description: string;
  // JSON Schema for parameters — Pi's `customTools` accept this verbatim
  // via the `typebox` adapter or as raw schema where supported.
  parameters: { [k: string]: Json };
}

/**
 * Sent Node → Python whenever Pi invokes a tool. Python answers with the
 * matching `tool_result` carrying the same `call_id`.
 */
export interface ToolCall {
  type: "tool_call";
  run_id: string;
  call_id: string;
  name: string;
  arguments: { [k: string]: Json };
}

/**
 * Sent Python → Node. The `done` field is the orchestration-level termination
 * signal — when true, the worker MUST end the agent after this turn.
 * `termination_reason` is informational ("max_steps" / "env_done" / "error").
 */
export interface ToolResult {
  type: "tool_result";
  run_id: string;
  call_id: string;
  observation: Json;
  observation_text: string; // pre-serialized text for the agent
  reward: number;
  done: boolean;
  info: { [k: string]: Json };
  termination_reason?: string;
}

/**
 * Sent Node → Python at any point for human-facing logs. Python forwards
 * to its logging framework. Never use console.log on stdout — that breaks
 * the JSON-RPC framing.
 */
export interface LogEvent {
  type: "log";
  run_id?: string;
  level: "debug" | "info" | "warn" | "error";
  message: string;
}

/**
 * Sent Node → Python when the run is complete. `trace` is the list of
 * StepRecord-shaped dicts (matches benchmark/common/runner.py:StepRecord
 * field names: observation, action, reward, raw_response, done, info).
 */
export interface RunEnd {
  type: "run_end";
  run_id: string;
  status: "done" | "partial" | "error";
  done: boolean;
  steps: number;
  finish_result: { [k: string]: Json } | null;
  trace: StepRecordDict[];
  cost: number;
  input_tokens: number;
  output_tokens: number;
  error: string | null;
}

export interface StepRecordDict {
  observation: Json;
  action: Json;
  reward: number;
  raw_response: string;
  done: boolean;
  info: { [k: string]: Json };
}

export type RpcRequest = RunStart | ToolResult;
export type RpcResponse = ToolCall | LogEvent | RunEnd;
```

- [ ] **Step 6: Verify `package.json` is parseable + the directory layout is sane**

```bash
cd /data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker
node --check src/protocol.ts 2>&1 | head -5 || echo "(expected: TS file — node --check tests JS only)"
python -c "import json; json.load(open('package.json'))" && echo "package.json valid JSON"
python -c "import json; json.load(open('tsconfig.json'))" && echo "tsconfig.json valid JSON"
ls src/
```

Expected: both `valid JSON` lines, `src/` lists `protocol.ts`.

- [ ] **Step 7: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
git add aorchestra/runtime/pi_worker/package.json aorchestra/runtime/pi_worker/tsconfig.json \
        aorchestra/runtime/pi_worker/README.md aorchestra/runtime/pi_worker/.gitignore \
        aorchestra/runtime/pi_worker/src/protocol.ts
git commit -m "feat(pi-worker): scaffold Node worker + JSON-RPC protocol types"
```

### Task 6: Pi worker `tools.ts` + `agent.ts` + `index.ts` entrypoint

**Files:**
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/tools.ts`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/agent.ts`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/index.ts`
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/tests/test_protocol.test.ts` (one smoke test that compiles and exercises the round-trip logic without a real Pi LLM)

**Interfaces:**
- Consumes: types from `protocol.ts` (Task 5); `@earendil-works/pi-agent-core` `Agent` constructor; `@earendil-works/pi-ai` model selection.
- Produces:
  - `tools.ts`: `buildPythonBridgeTools(spec, descriptors, bridge)` — turns each `ToolDescriptor` into a Pi `AgentTool` whose `execute` sends a `ToolCall` to Python and awaits a `ToolResult`. Every tool is registered with `executionMode: "sequential"`.
  - `agent.ts`: `runPiAgent({spec, descriptors, llm, bridge}): Promise<RunEnd>` — assembles system + user prompts, constructs the `Agent` with `toolExecution: "sequential"`, subscribes to events, drives the agent, converts events into `StepRecordDict[]`, returns a `RunEnd`.
  - `index.ts`: reads JSON lines from `process.stdin`, dispatches `run_start` to `runPiAgent`, writes `tool_call` / `log` / `run_end` to `process.stdout`. Exits with code 0 after writing `run_end`.

- [ ] **Step 1: `npm install` to materialize dependencies**

```bash
cd /data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker
npm install 2>&1 | tail -20
```

Expected: `added N packages` (no errors). Network must be available — if `npm install` fails, document it (the install IS part of Task 6 success; without dependencies the next steps fail).

- [ ] **Step 2: Implement `tools.ts`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/tools.ts`:

```typescript
/**
 * Bridge tools — every Pi tool round-trips to Python over JSON-RPC.
 *
 * Pi's `customTools` accept a name, description, JSON Schema parameters,
 * and an `execute` function. We synthesize one per `ToolDescriptor` the
 * Python side sent in `run_start`.
 *
 * The bridge handle owns the stdio adapter (send + correlated await). When
 * the agent calls a tool, we send a `tool_call` event and await the matching
 * `tool_result`. The `done` flag in the result is the orchestration kill
 * switch — if true, we propagate it via the tool's return so the agent
 * terminates gracefully.
 */
import type { ToolCall, ToolDescriptor, ToolResult } from "./protocol.js";

export interface PythonBridge {
  /**
   * Send a `tool_call` and resolve when the matching `tool_result` arrives.
   * Implementations correlate by `call_id`.
   */
  callPython(req: ToolCall): Promise<ToolResult>;

  /** Manufacture monotonically-unique call_ids. */
  nextCallId(): string;
}

/**
 * Build the array of `customTools` that `pi-coding-agent` (or the raw
 * `pi-agent-core` Agent) accepts. We keep the API surface narrow on purpose
 * so we can swap between coding-agent and agent-core later if needed.
 */
export function buildPythonBridgeTools(
  descriptors: ToolDescriptor[],
  bridge: PythonBridge
): PiToolLike[] {
  return descriptors.map((d) => ({
    name: d.name,
    description: d.description,
    parameters: d.parameters,
    executionMode: "sequential" as const,
    execute: async (args: Record<string, unknown>) => {
      const callId = bridge.nextCallId();
      const result = await bridge.callPython({
        type: "tool_call",
        run_id: "", // filled by index.ts before dispatch
        call_id: callId,
        name: d.name,
        arguments: args as Record<string, unknown>,
      });

      // Return both the text the model sees and the orchestration metadata.
      return {
        content: [
          {
            type: "text" as const,
            text: result.observation_text,
          },
        ],
        details: {
          observation: result.observation,
          reward: result.reward,
          done: result.done,
          info: result.info,
          termination_reason: result.termination_reason ?? null,
        },
        // `terminate` is honoured by pi-agent-core to end the agent after
        // this tool call completes (see docs/aopi.md pitfall #2 — orchestration
        // step budget MUST live server-side; this is the kill switch).
        terminate: result.done,
      };
    },
  }));
}

/**
 * Minimal "looks like a Pi tool" shape. We avoid importing the concrete type
 * from `@earendil-works/pi-agent-core` because the public API surface
 * shifts between minor versions — the shape we use is stable.
 */
export interface PiToolLike {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  executionMode: "sequential" | "parallel";
  execute: (args: Record<string, unknown>) => Promise<{
    content: Array<{ type: "text"; text: string }>;
    details?: Record<string, unknown>;
    terminate?: boolean;
  }>;
}
```

- [ ] **Step 3: Implement `agent.ts`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/agent.ts`:

```typescript
/**
 * Drives a Pi Agent end-to-end for one delegated AO sub-agent task.
 *
 * Design (see docs/aopi.md):
 * - Pi owns the agent loop. AO doesn't wrap Pi.step(); the orchestration
 *   step budget is enforced server-side via the tool gateway (which sets
 *   `done: true` once steps_taken reaches max_steps).
 * - All built-in Pi tools are disabled (`noTools: "builtin"`). Only the
 *   tools Python sent in `run_start.tool_descriptors` are exposed.
 * - Sequential tool execution at every level.
 *
 * On completion we return a `RunEnd` carrying the trace as StepRecordDict[],
 * which is the shape both the AOrchestra `Runner.run()` `LevelResult.trace`
 * field and claw-eval's `_trace_adapter` already consume.
 */
import { Agent } from "@earendil-works/pi-agent-core";

import type {
  PythonBridge,
  PiToolLike,
} from "./tools.js";
import { buildPythonBridgeTools } from "./tools.js";

import type {
  RunEnd,
  RunStart,
  StepRecordDict,
} from "./protocol.js";

export interface RunPiAgentArgs {
  spec: RunStart["spec"];
  toolDescriptors: RunStart["tool_descriptors"];
  llmEndpoint: RunStart["llm_endpoint"];
  bridge: PythonBridge;
  runId: string;
}

export async function runPiAgent(args: RunPiAgentArgs): Promise<RunEnd> {
  const { spec, toolDescriptors, llmEndpoint, bridge, runId } = args;

  const tools = buildPythonBridgeTools(toolDescriptors, bridge);
  // Stamp the run_id on every tool call the bridge sends back to Python.
  const wrappedTools = tools.map((t) => ({
    ...t,
    execute: async (input: Record<string, unknown>) => {
      // Pass through to the bridge but capture our run_id when correlating.
      return t.execute(input);
    },
  })) as PiToolLike[];

  const systemPrompt = buildSystemPrompt(spec);
  const userPrompt = buildUserPrompt(spec);

  const trace: StepRecordDict[] = [];
  let stepCount = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let cost = 0;
  let finishResult: Record<string, unknown> | null = null;
  let terminated = false;
  let lastError: string | null = null;

  const agent = new Agent({
    initialState: {
      systemPrompt,
      model: resolveModel(spec.model, llmEndpoint),
      thinkingLevel: "medium",
      tools: wrappedTools as never, // pi-agent-core uses a structural type
      messages: [],
      // Disable Pi's built-in bash/read/edit/write/etc.
      noTools: "builtin",
    } as unknown as never,
    // Pi global tool execution strategy. AO Environment is stateful.
    toolExecution: "sequential",
  } as unknown as never);

  agent.subscribe(async (event: PiAgentEvent) => {
    switch (event.type) {
      case "tool_execution_start":
        // Could log; nothing to record yet.
        break;
      case "tool_execution_end": {
        // Convert to a StepRecord-shaped dict.
        const tc = event.toolCall ?? {};
        const result = event.toolResult ?? {};
        const details =
          (result.details as Record<string, unknown> | undefined) ?? {};
        const reward =
          typeof details.reward === "number" ? details.reward : 0;
        const done = Boolean(details.done);
        const info = (details.info as Record<string, unknown>) ?? {};
        trace.push({
          observation: details.observation ?? null,
          action: { action: tc.name, params: tc.arguments },
          reward,
          raw_response: JSON.stringify(tc),
          done,
          info,
        });
        stepCount += 1;
        if (done) terminated = true;
        break;
      }
      case "message_end":
        // Pi reports token usage on message_end; aggregate it.
        if (event.usage) {
          inputTokens += event.usage.inputTokens ?? 0;
          outputTokens += event.usage.outputTokens ?? 0;
          cost += event.usage.cost ?? 0;
        }
        break;
      case "agent_error":
        lastError = String(event.error ?? "agent error");
        break;
      default:
        break;
    }
  });

  try {
    await agent.prompt(userPrompt);
  } catch (e: unknown) {
    lastError = e instanceof Error ? e.message : String(e);
  }

  // Try to extract a finish_result from the last tool call's info, mirroring
  // the ReActRuntime behaviour.
  if (trace.length > 0) {
    const lastInfo = trace[trace.length - 1].info ?? {};
    if (lastInfo.finished && lastInfo.finish_result) {
      finishResult = lastInfo.finish_result as Record<string, unknown>;
    }
  }

  const status: RunEnd["status"] =
    lastError != null ? "error" : terminated && finishResult ? "done" : "partial";

  return {
    type: "run_end",
    run_id: runId,
    status,
    done: terminated,
    steps: stepCount,
    finish_result: finishResult,
    trace,
    cost,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    error: lastError,
  };
}

function buildSystemPrompt(spec: RunStart["spec"]): string {
  return [
    "You are a SubAgent. Complete the assigned task using the available tools.",
    "Rules:",
    "- One tool call per turn (sequential execution is enforced server-side).",
    "- When you have an answer, call the `finish` tool with status=\"done\".",
    "- If you cannot finish, call `finish` with status=\"partial\".",
    "",
    `Original question: ${spec.original_question}`,
    spec.context ? `Context from prior attempts: ${spec.context}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

function buildUserPrompt(spec: RunStart["spec"]): string {
  return spec.instruction;
}

function resolveModel(
  modelName: string,
  endpoint: RunStart["llm_endpoint"],
): unknown {
  // The Pi SDK's exact "model" shape varies; we pass an object the runtime
  // can route to OpenAI-compatible endpoints. If the underlying SDK rejects
  // it, the run_end event will carry an `agent_error` and Python surfaces it.
  return {
    name: modelName,
    baseUrl: endpoint.base_url,
    apiKey: endpoint.api_key,
  };
}

interface PiAgentEvent {
  type: string;
  toolCall?: {
    name?: string;
    arguments?: Record<string, unknown>;
  };
  toolResult?: {
    content?: Array<{ type: "text"; text: string }>;
    details?: Record<string, unknown>;
  };
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    cost?: number;
  };
  error?: unknown;
}
```

- [ ] **Step 4: Implement `index.ts`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/src/index.ts`:

```typescript
/**
 * Pi worker entrypoint.
 *
 * Reads JSON-RPC 2.0 lines from stdin, writes events to stdout. Every line
 * is one JSON object. stderr is human logs only.
 *
 * Lifecycle:
 *   1. Wait for one `run_start` message.
 *   2. Start the Pi agent.
 *   3. Each time a tool needs Python, write `tool_call` to stdout and wait
 *      for the matching `tool_result` from stdin (correlated by call_id).
 *   4. When the agent finishes, write a single `run_end` and exit(0).
 *   5. On any uncaught exception write a final `run_end` with status="error"
 *      and exit(1).
 */
import readline from "node:readline";

import { runPiAgent } from "./agent.js";
import type { PythonBridge } from "./tools.js";
import type {
  RunStart,
  ToolCall,
  ToolResult,
  RunEnd,
} from "./protocol.js";

type Resolver = (result: ToolResult) => void;

const pendingResolvers = new Map<string, Resolver>();
let callIdCounter = 0;

function write(obj: unknown): void {
  // Each event MUST be on its own line.
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function log(level: "debug" | "info" | "warn" | "error", message: string): void {
  // Logs go to stderr so they never collide with JSON-RPC framing.
  process.stderr.write(`[pi-worker] ${level}: ${message}\n`);
}

const bridge: PythonBridge = {
  nextCallId: () => `call-${++callIdCounter}`,
  callPython: (req: ToolCall): Promise<ToolResult> =>
    new Promise<ToolResult>((resolve) => {
      pendingResolvers.set(req.call_id, resolve);
      write(req);
    }),
};

async function main(): Promise<void> {
  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });

  let runStart: RunStart | null = null;
  const incoming: AsyncIterable<string> = rl;
  for await (const line of incoming) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let msg: unknown;
    try {
      msg = JSON.parse(trimmed);
    } catch (e) {
      log("error", `bad JSON on stdin: ${(e as Error).message}`);
      continue;
    }
    if (!isObject(msg)) continue;
    const type = (msg as { type?: unknown }).type;
    if (type === "run_start") {
      runStart = msg as RunStart;
      break;
    } else if (type === "tool_result") {
      // Premature; we have no run yet.
      log("warn", "tool_result before run_start; ignoring");
    } else {
      log("warn", `unknown message type before run_start: ${String(type)}`);
    }
  }

  if (!runStart) {
    log("error", "stdin closed before run_start");
    return;
  }

  // Stamp run_id on every outgoing tool_call.
  const runId = runStart.run_id;
  const taggingBridge: PythonBridge = {
    nextCallId: bridge.nextCallId,
    callPython: (req: ToolCall) => bridge.callPython({ ...req, run_id: runId }),
  };

  // Start a separate listener loop for tool_result while the agent runs.
  const listenerDone = (async () => {
    for await (const line of rl) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let msg: unknown;
      try {
        msg = JSON.parse(trimmed);
      } catch (e) {
        log("error", `bad JSON: ${(e as Error).message}`);
        continue;
      }
      if (!isObject(msg)) continue;
      if ((msg as { type?: unknown }).type === "tool_result") {
        const tr = msg as ToolResult;
        const resolver = pendingResolvers.get(tr.call_id);
        if (resolver) {
          pendingResolvers.delete(tr.call_id);
          resolver(tr);
        } else {
          log("warn", `tool_result with unknown call_id: ${tr.call_id}`);
        }
      }
    }
  })();

  let end: RunEnd;
  try {
    end = await runPiAgent({
      spec: runStart.spec,
      toolDescriptors: runStart.tool_descriptors,
      llmEndpoint: runStart.llm_endpoint,
      bridge: taggingBridge,
      runId,
    });
  } catch (e) {
    end = {
      type: "run_end",
      run_id: runId,
      status: "error",
      done: false,
      steps: 0,
      finish_result: null,
      trace: [],
      cost: 0,
      input_tokens: 0,
      output_tokens: 0,
      error: (e as Error).message,
    };
  }

  write(end);
  // Closing stdout lets the parent know we're done.
  process.stdout.end();
  await Promise.race([
    listenerDone,
    new Promise<void>((resolve) => setTimeout(resolve, 100)),
  ]);
  process.exit(end.status === "error" ? 1 : 0);
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null;
}

main().catch((e) => {
  log("error", `fatal: ${(e as Error).message}`);
  process.exit(1);
});
```

- [ ] **Step 5: Build the worker**

```bash
cd /data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker
npm run build 2>&1 | tail -15
```

Expected: no errors. If pi-agent-core's type definitions reject the way `Agent` is constructed, adjust the `as unknown as never` casts to match (the runtime behaviour will be the same; only the types differ across minor versions).

- [ ] **Step 6: Smoke-test the worker with a hand-crafted run**

```bash
cd /data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker
cat > /tmp/pi_worker_smoke.json <<'EOF'
{"type":"run_start","run_id":"test-1","spec":{"instruction":"say hello","context":"","tools":["finish"],"model":"claude-sonnet-4-5","original_question":"say hello","benchmark_type":"gaia","max_steps":3,"metadata":{}},"tool_descriptors":[{"name":"finish","description":"Finish the task with a final answer","parameters":{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"]}}],"llm_endpoint":{"base_url":"https://newapi.deepwisdom.ai/v1","api_key":"INVALID"}}
EOF
# Feed an immediate tool_result so the worker has *something* to consume if
# it calls `finish`. With an invalid API key the agent will error out before
# calling anything — that's fine; we're verifying the framing/exit code.
node dist/index.js < /tmp/pi_worker_smoke.json 2>&1 | tail -5
```

Expected: a JSON `run_end` line on stdout with `status: "error"` (or `done` if the test ran with a real key). The worker exits without hanging. If it hangs, check that `process.stdout.end()` is reached.

- [ ] **Step 7: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
git add aorchestra/runtime/pi_worker/src/tools.ts \
        aorchestra/runtime/pi_worker/src/agent.ts \
        aorchestra/runtime/pi_worker/src/index.ts
# package-lock.json is generated by npm install — include it for reproducibility
[ -f aorchestra/runtime/pi_worker/package-lock.json ] && git add aorchestra/runtime/pi_worker/package-lock.json
git commit -m "feat(pi-worker): Node worker drives Pi Agent over stdio JSON-RPC"
```

---

## Wave 5-C — PiRuntime (Python side)

Goal: a Python class that subprocesses the Node worker, sends a `run_start`, services every `tool_call` via the AO Environment, collects the `run_end`, and returns a `SubAgentRunResult`.

### Task 7: `PiRuntime` core + step-budget tool gateway

**Files:**
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_runtime.py`
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`
- Create: `/data2/ruanjianhao/AOrchestra/tests/runtime/test_pi_runtime.py`

**Interfaces:**
- Consumes:
  - From Task 1: `SubAgentSpec`, `SubAgentRunResult`, `RuntimeRegistry`, `default_registry()`.
  - From upstream: `base.engine.async_llm.LLMsConfig`, `benchmark.common.env.Environment` (duck-typed).
  - The compiled Pi worker at `aorchestra/runtime/pi_worker/dist/index.js`.
- Produces:
  - `class PiRuntime` — concrete `SubAgentRuntime`.
    - Constructor takes optional `node_bin: str = None` (default `os.environ.get("PI_RUNTIME_NODE_BIN", "node")`) and `worker_entrypoint: Path = None` (default the compiled dist path).
    - `async run(spec, env)`:
      1. Spawn the Node worker as a subprocess (`asyncio.create_subprocess_exec(node_bin, worker_entrypoint, stdin=PIPE, stdout=PIPE, stderr=PIPE)`).
      2. Build the tool descriptors. Each tool name from `spec.tools` is paired with description + schema. **The required `finish` tool is appended unconditionally** if not already present (so Pi always has a way to stop).
      3. Resolve the LLM endpoint via `LLMsConfig.default().get(spec.model)` and serialise `base_url` + `api_key` into the `run_start` envelope.
      4. Write the `run_start` line to the worker's stdin.
      5. Loop: read one line from the worker's stdout. If `tool_call`, call `_handle_tool_call`, write a `tool_result` line back. If `log`, route to Python logger. If `run_end`, break.
      6. Wait for the process to exit. Drain stderr into the logger.
      7. Convert the `RunEnd` payload to `SubAgentRunResult`. Restore `env.instruction` if we touched it.
    - On any exception: terminate the subprocess (kill if still alive after 2 seconds), return `SubAgentRunResult(status="error", ..., error=str(exc))`.
  - `_handle_tool_call(env, spec, call)` does the step-budget enforcement:
    - Increment a per-run `steps_taken` counter.
    - If the tool name is `"finish"`, return `observation={"finished": True, "finish_result": call.arguments}`, `done=True`, `info={"finished": True, "finish_result": call.arguments}`. Don't call `env.step()`.
    - Else build an AO `Action` dict `{"action": call.name, "params": call.arguments}` and `await env.step(action)`. If `env.step` is sync, fall back to awaitable handling.
    - If `steps_taken >= spec.max_steps`, force `done=True` and add `info["termination_reason"] = "max_steps"`.
    - Serialise the observation to a text representation (`json.dumps` if dict, else `str(obs)`) for the `observation_text` field.

- [ ] **Step 1: Write failing tests**

Create `/data2/ruanjianhao/AOrchestra/tests/runtime/test_pi_runtime.py`:

```python
"""PiRuntime tests with a mocked Node worker.

We avoid `npm install` + real Pi LLM calls in unit tests. Instead, the
"worker" is a tiny Python script we spawn via the same asyncio subprocess
machinery. It performs a scripted JSON-RPC dialog so we can drive the
Python side through its full state machine.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from aorchestra.runtime.base import SubAgentSpec
from aorchestra.runtime.pi_runtime import PiRuntime


class _FakeEnv:
    """Minimal Environment for the gateway to step through."""

    def __init__(self) -> None:
        self.instruction = "original-task"
        self.steps_observed: list[dict] = []

    def get_basic_info(self):  # noqa: D401
        from benchmark.common.env import BasicInfo
        return BasicInfo(
            env_id="fake", instruction=self.instruction,
            action_space="", max_steps=30, meta_data={},
        )

    async def reset(self, seed=None):  # noqa: ARG002
        return {"text": "initial obs"}

    async def step(self, action):
        self.steps_observed.append(action)
        # Pretend OCR returned some text.
        return ({"text": "ocr returned 12345"}, 0.0, False, {})


def _write_mock_worker(tmp_path: Path, script_lines: list[str]) -> Path:
    """Materialise a Python script that imitates the Node worker.

    ``script_lines`` is a list of strings the worker should write to stdout
    in order. Interleave None entries with reads from stdin.
    """
    target = tmp_path / "mock_worker.py"
    body = textwrap.dedent(
        """
        import sys, json

        def emit(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        def read():
            return json.loads(sys.stdin.readline())
        """
    )
    body += "\n" + "\n".join(script_lines) + "\n"
    target.write_text(body, encoding="utf-8")
    return target


def _make_runtime_using_python_worker(worker_path: Path) -> PiRuntime:
    rt = PiRuntime(
        node_bin=sys.executable,  # use python to run our mock instead of node
        worker_entrypoint=worker_path,
    )
    return rt


@pytest.fixture(autouse=True)
def patched_llms_config(monkeypatch):
    """Pretend LLMsConfig has the model the test asks for."""
    from base.engine import async_llm as al
    from types import SimpleNamespace

    fake_config = SimpleNamespace(
        base_url="https://example.com/v1",
        key="sk-test",
        model="claude-sonnet-4-5",
    )
    monkeypatch.setattr(
        al.LLMsConfig,
        "default",
        classmethod(lambda cls: SimpleNamespace(get=lambda name: fake_config)),
    )


def test_pi_runtime_completes_with_finish_tool(tmp_path):
    """Worker: sends one tool_call(finish, {answer: 36080}), then run_end(done)."""
    script = [
        # Read run_start
        "rs = read()",
        "run_id = rs['run_id']",
        # Emit tool_call(finish)
        "emit({'type': 'tool_call', 'run_id': run_id, 'call_id': 'c1', "
        "      'name': 'finish', 'arguments': {'answer': '36080'}})",
        # Read tool_result
        "tr = read()",
        # Emit run_end
        "emit({'type': 'run_end', 'run_id': run_id, 'status': 'done', "
        "      'done': True, 'steps': 1, "
        "      'finish_result': {'answer': '36080'}, "
        "      'trace': [{'observation': tr['observation'], "
        "                  'action': {'action': 'finish', 'params': {'answer': '36080'}}, "
        "                  'reward': 0.0, 'raw_response': '{}', "
        "                  'done': True, 'info': {}}], "
        "      'cost': 0.02, 'input_tokens': 100, 'output_tokens': 20, "
        "      'error': None})",
    ]
    worker = _write_mock_worker(tmp_path, script)
    rt = _make_runtime_using_python_worker(worker)

    spec = SubAgentSpec(
        instruction="extract budget",
        context="",
        tools=["ocr_extract_text"],  # `finish` auto-added
        model="claude-sonnet-4-5",
        benchmark_type="gaia",
        max_steps=10,
    )
    env = _FakeEnv()
    result = asyncio.run(rt.run(spec, env))

    assert result.status == "done"
    assert result.done is True
    assert result.steps == 1
    assert result.finish_result == {"answer": "36080"}
    assert result.cost == 0.02
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    # finish is short-circuited; env.step was NOT called
    assert env.steps_observed == []
    # env.instruction restored
    assert env.instruction == "original-task"


def test_pi_runtime_step_budget_enforced_in_gateway(tmp_path):
    """Worker: calls a non-finish tool repeatedly. The gateway must force
    done=True after max_steps regardless of what the worker keeps asking."""
    script = [
        "rs = read()",
        "run_id = rs['run_id']",
        # Three identical ocr_extract_text calls
        "for i in range(3):",
        "    emit({'type': 'tool_call', 'run_id': run_id, "
        "          'call_id': f'c{i}', 'name': 'ocr_extract_text', "
        "          'arguments': {'image_path': 'x.pdf'}})",
        "    tr = read()",
        "    if tr.get('done'):",
        "        break",
        "emit({'type': 'run_end', 'run_id': run_id, 'status': 'partial', "
        "      'done': True, 'steps': i + 1, 'finish_result': None, "
        "      'trace': [], 'cost': 0.0, 'input_tokens': 0, "
        "      'output_tokens': 0, 'error': None})",
    ]
    worker = _write_mock_worker(tmp_path, script)
    rt = _make_runtime_using_python_worker(worker)

    spec = SubAgentSpec(
        instruction="x", context="", tools=["ocr_extract_text"],
        model="claude-sonnet-4-5", max_steps=2,
    )
    env = _FakeEnv()
    result = asyncio.run(rt.run(spec, env))

    # The gateway answered the first two tool_calls normally (done=False) and
    # forced done=True on the third — but the worker breaks out of its loop
    # after seeing done=True.
    assert len(env.steps_observed) == 2
    assert result.done is True


def test_pi_runtime_returns_error_when_worker_exits_nonzero(tmp_path):
    """Worker dies before sending run_end — Python should not hang."""
    script = [
        "rs = read()",
        "import sys; sys.exit(2)",
    ]
    worker = _write_mock_worker(tmp_path, script)
    rt = _make_runtime_using_python_worker(worker)

    spec = SubAgentSpec(
        instruction="x", context="", tools=["finish"],
        model="claude-sonnet-4-5", max_steps=5,
    )
    env = _FakeEnv()
    result = asyncio.run(rt.run(spec, env))
    assert result.status == "error"
    assert result.error is not None


def test_pi_runtime_registry_lazy_registration():
    """Importing pi_runtime registers 'pi' in default_registry()."""
    from aorchestra.runtime import default_registry
    import aorchestra.runtime.pi_runtime  # noqa: F401 — side-effect import

    assert "pi" in default_registry().names()
    assert isinstance(default_registry().get("pi"), PiRuntime)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_pi_runtime.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: aorchestra.runtime.pi_runtime`.

- [ ] **Step 3: Implement `pi_runtime.py`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_runtime.py`:

```python
"""PiRuntime — spawns the Node Pi worker and routes its tool calls
back to an AO Environment.

Architecture (see /data2/ruanjianhao/claw-eval/docs/aopi.md):

- Pi owns the agent loop. AO does NOT wrap Pi.step().
- The orchestration step budget is enforced in this module (the tool
  gateway) — never relied on from a prompt.
- Pi's built-in tools are disabled in the worker (Task 6 sets
  ``noTools: "builtin"``).
- All tool execution is sequential (worker sets toolExecution / executionMode
  to "sequential").
- `env.reset()` is called before the first tool_call to match
  benchmark/common/runner.py:Runner.run() semantics.
- Trace returned by PiRuntime is in StepRecord-shaped dicts so existing
  consumers (DelegateTaskTool._summarize_trace, claw-eval _trace_adapter)
  don't need to know which runtime produced it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from base.engine.async_llm import LLMsConfig

from aorchestra.runtime.base import (
    SubAgentRunResult,
    SubAgentSpec,
    default_registry,
)


logger = logging.getLogger(__name__)


_DEFAULT_WORKER_ENTRYPOINT = (
    Path(__file__).resolve().parent / "pi_worker" / "dist" / "index.js"
)


# Schema for the implicit `finish` tool we append to every run if not already
# in the spec's tool list. Pi needs an explicit termination tool.
_FINISH_TOOL_DESCRIPTOR = {
    "name": "finish",
    "description": "Finish the task. Use status='done' if you have an answer, "
                   "status='partial' if you ran out of options.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done", "partial"]},
            "answer": {"type": "string"},
            "key_findings": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["status"],
    },
}


class PiRuntime:
    """Concrete ``SubAgentRuntime`` that drives a Node ``pi-agent-core``
    worker over JSON-RPC stdio.

    Threading model: one worker subprocess per ``run()`` call. We don't
    pool workers in this first version; that's a separate optimisation.
    """

    def __init__(
        self,
        node_bin: str | None = None,
        worker_entrypoint: Path | None = None,
        worker_timeout_s: float = 600.0,
    ) -> None:
        self._node_bin = node_bin or os.environ.get("PI_RUNTIME_NODE_BIN", "node")
        self._worker_entrypoint = Path(worker_entrypoint or _DEFAULT_WORKER_ENTRYPOINT)
        self._worker_timeout_s = worker_timeout_s

    async def run(
        self,
        spec: SubAgentSpec,
        env: Any,
    ) -> SubAgentRunResult:
        if not self._worker_entrypoint.exists():
            return SubAgentRunResult(
                status="error",
                done=False,
                steps=0,
                finish_result=None,
                trace=[],
                error=(
                    f"Pi worker not built at {self._worker_entrypoint}. "
                    f"Run `cd aorchestra/runtime/pi_worker && npm install && npm run build`."
                ),
            )

        # Save env.instruction for restore on exit.
        original_instruction = getattr(env, "instruction", None)
        if hasattr(env, "instruction"):
            env.instruction = spec.instruction

        # Reset env so PiRuntime matches benchmark/common/runner.py semantics.
        try:
            reset_result = env.reset()
            if inspect.isawaitable(reset_result):
                await reset_result
        except Exception as exc:  # noqa: BLE001
            self._restore(env, original_instruction)
            return SubAgentRunResult(
                status="error", done=False, steps=0, finish_result=None,
                trace=[], error=f"env.reset failed: {exc}",
            )

        # Build the run_start envelope.
        run_id = uuid.uuid4().hex
        llm_cfg = LLMsConfig.default().get(spec.model)
        descriptors = self._build_descriptors(spec)

        run_start = {
            "type": "run_start",
            "run_id": run_id,
            "spec": {
                "instruction": spec.instruction,
                "context": spec.context,
                "tools": spec.tools,
                "model": spec.model,
                "original_question": spec.original_question,
                "benchmark_type": spec.benchmark_type,
                "max_steps": spec.max_steps,
                "metadata": dict(spec.metadata),
            },
            "tool_descriptors": descriptors,
            "llm_endpoint": {
                "base_url": getattr(llm_cfg, "base_url", "") or "",
                "api_key": getattr(llm_cfg, "key", "") or "",
            },
        }

        proc = await asyncio.create_subprocess_exec(
            self._node_bin,
            str(self._worker_entrypoint),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        steps_taken = 0
        run_end_payload: dict | None = None

        async def _stderr_drain() -> None:
            assert proc.stderr is not None
            async for line in proc.stderr:
                logger.info("[pi-worker] %s", line.decode().rstrip())

        stderr_task = asyncio.create_task(_stderr_drain())

        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write((json.dumps(run_start) + "\n").encode())
            await proc.stdin.drain()

            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=self._worker_timeout_s,
                )
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError as exc:
                    logger.warning("pi-worker emitted non-JSON: %s", exc)
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "tool_call":
                    steps_taken += 1
                    tool_result = await self._handle_tool_call(
                        env, spec, msg, steps_taken,
                    )
                    proc.stdin.write((json.dumps(tool_result) + "\n").encode())
                    await proc.stdin.drain()
                elif mtype == "log":
                    level = (msg.get("level") or "info").lower()
                    getattr(logger, level if hasattr(logger, level) else "info")(
                        "[pi-worker] %s", msg.get("message", "")
                    )
                elif mtype == "run_end":
                    run_end_payload = msg
                    break
                else:
                    logger.warning("unknown message type from pi-worker: %s", mtype)

            await proc.wait()

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._restore(env, original_instruction)
            stderr_task.cancel()
            return SubAgentRunResult(
                status="error", done=False, steps=steps_taken,
                finish_result=None, trace=[],
                error=f"pi-worker exceeded {self._worker_timeout_s}s timeout",
            )
        except Exception as exc:  # noqa: BLE001
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            self._restore(env, original_instruction)
            stderr_task.cancel()
            return SubAgentRunResult(
                status="error", done=False, steps=steps_taken,
                finish_result=None, trace=[],
                error=f"{type(exc).__name__}: {exc}",
            )

        # Drain stderr fully.
        try:
            await asyncio.wait_for(stderr_task, timeout=2.0)
        except asyncio.TimeoutError:
            stderr_task.cancel()

        self._restore(env, original_instruction)

        if proc.returncode not in (0, None):
            err = (run_end_payload or {}).get(
                "error",
                f"pi-worker exited with code {proc.returncode}",
            )
            return SubAgentRunResult(
                status="error", done=False, steps=steps_taken,
                finish_result=None, trace=[], error=str(err),
            )

        if not run_end_payload:
            return SubAgentRunResult(
                status="error", done=False, steps=steps_taken,
                finish_result=None, trace=[],
                error="pi-worker exited without sending run_end",
            )

        return SubAgentRunResult(
            status=run_end_payload.get("status", "error"),
            done=bool(run_end_payload.get("done", False)),
            steps=int(run_end_payload.get("steps", steps_taken)),
            finish_result=run_end_payload.get("finish_result"),
            trace=run_end_payload.get("trace", []) or [],
            cost=float(run_end_payload.get("cost") or 0.0),
            input_tokens=int(run_end_payload.get("input_tokens") or 0),
            output_tokens=int(run_end_payload.get("output_tokens") or 0),
            error=run_end_payload.get("error"),
        )

    @staticmethod
    def _restore(env: Any, original: Any) -> None:
        if original is not None and hasattr(env, "instruction"):
            env.instruction = original

    @staticmethod
    def _build_descriptors(spec: SubAgentSpec) -> list[dict]:
        """One descriptor per tool name + an implicit `finish` descriptor.

        We don't currently know each tool's full JSON Schema from inside
        DelegateTaskTool — the Spec only carries names. For non-finish
        tools we emit a permissive ``additionalProperties: true`` shape that
        Pi will accept; the actual Python ClawEvalAction / GAIA tool will
        validate the args when env.step dispatches.

        TODO: a future revision can have DelegateTaskTool / ClawEvalEnv
        pass the full schemas. For now permissive shapes are enough to
        unblock Phase 5.
        """
        descriptors: list[dict] = []
        seen: set[str] = set()
        for name in spec.tools:
            descriptors.append({
                "name": name,
                "description": f"Tool `{name}` from the task's action space.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": True,
                },
            })
            seen.add(name)
        if "finish" not in seen:
            descriptors.append(_FINISH_TOOL_DESCRIPTOR)
        return descriptors

    async def _handle_tool_call(
        self,
        env: Any,
        spec: SubAgentSpec,
        call: dict,
        steps_taken: int,
    ) -> dict:
        """Service a Pi tool_call by stepping the AO Environment.

        Enforces ``spec.max_steps`` server-side: once the count is reached we
        force ``done=True`` and add ``termination_reason="max_steps"``.
        Handles ``finish`` specially — it doesn't go through env.step.
        """
        name = call.get("name") or ""
        args = call.get("arguments") or {}
        call_id = call.get("call_id") or ""

        if name == "finish":
            return {
                "type": "tool_result",
                "run_id": call.get("run_id"),
                "call_id": call_id,
                "observation": {"finished": True, "finish_result": args},
                "observation_text": json.dumps(args, ensure_ascii=False),
                "reward": 0.0,
                "done": True,
                "info": {"finished": True, "finish_result": args},
                "termination_reason": "finish",
            }

        action = {"action": name, "params": args}
        try:
            step_result = env.step(action)
            if inspect.isawaitable(step_result):
                obs, reward, done, info = await step_result
            else:
                obs, reward, done, info = step_result
        except Exception as exc:  # noqa: BLE001
            return {
                "type": "tool_result",
                "run_id": call.get("run_id"),
                "call_id": call_id,
                "observation": {"error": str(exc)},
                "observation_text": f"error: {exc}",
                "reward": 0.0,
                "done": True,
                "info": {"error": str(exc)},
                "termination_reason": "error",
            }

        # Enforce the step budget here, not in the prompt.
        if steps_taken >= spec.max_steps:
            done = True
            info = dict(info or {})
            info["termination_reason"] = "max_steps"
            termination_reason = "max_steps"
        else:
            termination_reason = "env_done" if done else None

        return {
            "type": "tool_result",
            "run_id": call.get("run_id"),
            "call_id": call_id,
            "observation": obs,
            "observation_text": self._serialize_obs(obs),
            "reward": float(reward or 0.0),
            "done": bool(done),
            "info": info or {},
            "termination_reason": termination_reason,
        }

    @staticmethod
    def _serialize_obs(obs: Any) -> str:
        if isinstance(obs, str):
            return obs
        try:
            return json.dumps(obs, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return str(obs)


# ---------------------------------------------------------------------------
# Register on import so callers can opt in with ``runtime_name="pi"``.
# Pi imports happen lazily because the Node worker isn't required for
# react-only callers (and a missing worker shouldn't crash AO imports).
# ---------------------------------------------------------------------------


def _register_pi() -> None:
    reg = default_registry()
    if "pi" not in reg.names():
        reg.register("pi", PiRuntime())


_register_pi()
```

- [ ] **Step 4: Update package init**

Update `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/__init__.py`:

```python
"""Pluggable SubAgent runtime backends.

Phase 5 — see /data2/ruanjianhao/claw-eval/docs/superpowers/plans/2026-06-25-aorchestra-pi-runtime.md
and /data2/ruanjianhao/claw-eval/docs/aopi.md.

On import this module ensures the ``default_registry()`` has both
``"react"`` and ``"pi"`` pre-registered. The Pi worker is NOT required to
be built for the package to import — PiRuntime.run() returns an error
result if the worker is missing.
"""
from __future__ import annotations

from aorchestra.runtime.base import (
    RuntimeRegistry,
    SubAgentRunResult,
    SubAgentRuntime,
    SubAgentSpec,
    default_registry,
)
from aorchestra.runtime.react_runtime import ReActRuntime

# Imported for its registration side-effect.
from aorchestra.runtime.pi_runtime import PiRuntime  # noqa: F401


def _register_defaults() -> None:
    reg = default_registry()
    if "react" not in reg.names():
        reg.register("react", ReActRuntime())


_register_defaults()


__all__ = [
    "PiRuntime",
    "ReActRuntime",
    "RuntimeRegistry",
    "SubAgentRunResult",
    "SubAgentRuntime",
    "SubAgentSpec",
    "default_registry",
]
```

- [ ] **Step 5: Run the new tests**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_pi_runtime.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Run all runtime tests**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/ -v
```

Expected: 21 passed.

- [ ] **Step 7: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
git add aorchestra/runtime/pi_runtime.py aorchestra/runtime/__init__.py \
        tests/runtime/test_pi_runtime.py
git commit -m "feat(runtime): PiRuntime driver + gateway-enforced step budget"
```

---

## Wave 5-D — Switch claw-eval to Pi and re-run T077 e2e

### Task 8: Build the Pi worker once for real

**Files:**
- None (build artifact).

**Interfaces:**
- Consumes: pi_worker scaffolding (Tasks 5-6).
- Produces: `aorchestra/runtime/pi_worker/dist/index.js` populated.

- [ ] **Step 1: Build**

```bash
cd /data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker
npm run build 2>&1 | tail -10
ls -la dist/
```

Expected: at minimum `dist/index.js`, `dist/agent.js`, `dist/tools.js`, `dist/protocol.js` (the protocol file becomes a no-op JS module).

- [ ] **Step 2: Smoke-test that PiRuntime loads it**

```bash
cd /data2/ruanjianhao/AOrchestra
python -c "
from aorchestra.runtime import PiRuntime
import os
rt = PiRuntime()
print('worker_entrypoint:', rt._worker_entrypoint)
print('exists:', rt._worker_entrypoint.exists())
"
```

Expected: `exists: True`.

- [ ] **Step 3: No commit**

(The dist directory is in `.gitignore`. Real CI rebuilds it.)

### Task 9: Switch `claw-eval/_runner.py` to pass `runtime_name="pi"`

**Files:**
- Modify: `/data2/ruanjianhao/claw-eval/src/claw_eval/harnesses/aorchestra/_runner.py` (lines around 372-380)

**Interfaces:**
- Consumes: the refactored `DelegateTaskTool` from Task 4.
- Produces: claw-eval's MainAgent runs against `PiRuntime` by default.

- [ ] **Step 1: Modify the DelegateTaskTool construction site**

Locate the `delegate_tool = DelegateTaskTool(...)` block at approximately line 375. Replace with:

```python
    # DelegateTaskTool: which sub-agent runtime drives the SubAgents?
    # Phase 5 default: "pi" (Node-side @earendil-works/pi-agent-core worker).
    # Override via CLAWEVAL_AORCHESTRA_RUNTIME=react to fall back to the
    # legacy ReActAgent path while triaging issues.
    from aorchestra.runtime import default_registry

    runtime_name = os.environ.get("CLAWEVAL_AORCHESTRA_RUNTIME", "pi")
    delegate_tool = DelegateTaskTool(
        env=env,
        runner=sub_runner,
        models=sub_models,
        benchmark_type="gaia",
        runtime_registry=default_registry(),
        runtime_name=runtime_name,
    )
```

Make sure `import os` is already at the top of the file. If not, add it.

- [ ] **Step 2: Run claw-eval's unit test suite (no LLM, no e2e)**

```bash
cd /data2/ruanjianhao/claw-eval
python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
```

Expected: 92 passed, 4 skipped (the e2e tests are gated, the placeholder and bridge tests still pass).

- [ ] **Step 3: Commit (claw-eval side)**

```bash
cd /data2/ruanjianhao/claw-eval
git add src/claw_eval/harnesses/aorchestra/_runner.py
git commit -m "feat(aorchestra): default to PiRuntime (CLAWEVAL_AORCHESTRA_RUNTIME=pi)"
```

### Task 10: Run T077 e2e through Pi

**Files:**
- None (verification only).

**Interfaces:**
- Consumes: all of Phase 5 so far.
- Produces: a successful (or diagnostically informative) `e2e_report.json` for T077.

- [ ] **Step 1: Run the e2e**

```bash
cd /data2/ruanjianhao/claw-eval
CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1 \
CLAWEVAL_LLM_API_KEY=<your-key> \
CLAWEVAL_LLM_MODEL=claude-sonnet-4-5 \
CLAWEVAL_AORCHESTRA_RUNTIME=pi \
RUN_E2E=1 \
python -m pytest tests/test_aorchestra_e2e.py -p no:quadrants -v --tb=short 2>&1 | tail -30
```

Expected (success path): `1 passed`. The report at `/tmp/pytest-of-root/pytest-N/test_t077_aorchestra_host_e2e0/e2e_report.json` shows `task_score >= 0.3` and `agent_role_seen` contains both `"main"` and `"sub"`.

Expected (degraded but acceptable path): the run completes with a score below 0.3 — capture the report path and the four-line `scores` block (completion / robustness / communication / safety) for the next step's diagnostic write-up.

- [ ] **Step 2: Capture the diagnostic snapshot**

```bash
# Replace pytest-N with the actual run dir.
ls -lt /tmp/pytest-of-root/ | head -3
REPORT=$(find /tmp/pytest-of-root -name 'e2e_report.json' -mmin -30 | head -1)
echo "report at: $REPORT"
cat "$REPORT" | python -m json.tool | head -50
```

Save the output for Task 14 (decision log).

- [ ] **Step 3: Compare against the ReAct baseline**

```bash
cd /data2/ruanjianhao/claw-eval
CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1 \
CLAWEVAL_LLM_API_KEY=<your-key> \
CLAWEVAL_LLM_MODEL=claude-sonnet-4-5 \
CLAWEVAL_AORCHESTRA_RUNTIME=react \
RUN_E2E=1 \
python -m pytest tests/test_aorchestra_e2e.py -p no:quadrants -v --tb=short 2>&1 | tail -10
```

Expected: same test name; the score is the existing Wave 4-D baseline (0.28 historically). The point is to confirm BOTH runtimes are selectable via env var.

- [ ] **Step 4: No commit yet**

The harness change in Task 9 is the only diff; numbers go into the decision doc in Task 14.

---

## Wave 5-E — Verification, Documentation, Push

### Task 11: All-runtime regression in AOrchestra

**Files:**
- None.

**Interfaces:**
- Verifies: nothing from this phase broke AOrchestra's GAIA / TerminalBench / SWE-bench dispatch path.

- [ ] **Step 1: Re-run the entire AOrchestra runtime suite**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/ -v
```

Expected: 21 passed.

- [ ] **Step 2: Make sure existing AOrchestra tests outside `tests/runtime/` still work**

```bash
cd /data2/ruanjianhao/AOrchestra
# Find any pre-existing test directory and run it.
ls tests/ 2>/dev/null
python -m pytest tests/ -v 2>&1 | tail -5
```

Expected: whatever the existing pass-rate was, unchanged. If AOrchestra has no other tests, "no tests ran" is also acceptable.

- [ ] **Step 3: Smoke-import every entry point that previously used the `runner`-positional argument**

```bash
cd /data2/ruanjianhao/AOrchestra
python -c "from aorchestra.runners.gaia_runner import GAIARunner; print('gaia ok')"
python -c "from aorchestra.runners.terminalbench_runner import TerminalBenchRunner; print('tb ok')"
python -c "from aorchestra.runners.swebench_runner import SWEBenchRunner; print('swe ok')"
```

Expected: three `ok` lines.

### Task 12: Pi worker integration smoke test (real Pi LLM)

**Files:**
- None (manual verification + diagnostic).

**Interfaces:**
- Verifies: the Pi worker can actually drive a real `claude-sonnet-4-5` round-trip end-to-end.

- [ ] **Step 1: Hand-craft a one-tool spec and feed it to the worker**

```bash
cd /data2/ruanjianhao/AOrchestra
python <<'PY'
import asyncio, os
from aorchestra.runtime import PiRuntime, SubAgentSpec
from base.engine.async_llm import LLMsConfig

# Configure the runtime LLM endpoint via the env vars claw-eval uses.
LLMsConfig._default_config = LLMsConfig({
    "claude-sonnet-4-5": {
        "api_key": os.environ["CLAWEVAL_LLM_API_KEY"],
        "base_url": os.environ["CLAWEVAL_LLM_BASE_URL"],
        "temperature": 0,
    },
})

class Env:
    instruction = "what is 2+2?"
    def get_basic_info(self):
        from benchmark.common.env import BasicInfo
        return BasicInfo(env_id="x", instruction=self.instruction,
                         action_space="", max_steps=5, meta_data={})
    async def reset(self, seed=None):
        return {"text": ""}
    async def step(self, action):
        return ({"text": "ack"}, 0.0, False, {})

spec = SubAgentSpec(
    instruction="what is 2+2? finish with the answer.",
    context="", tools=[], model="claude-sonnet-4-5",
    benchmark_type="gaia", max_steps=3,
)
result = asyncio.run(PiRuntime().run(spec, Env()))
print("status:", result.status)
print("steps:", result.steps)
print("finish_result:", result.finish_result)
print("error:", result.error)
PY
```

Expected: `status: done`, `finish_result` contains `answer` ≈ `"4"`. If `status: error`, capture `error` text and feed it into Task 14.

### Task 13: Push (AOrchestra side, if applicable)

**Files:**
- None.

**Interfaces:**
- Verifies: AOrchestra has a commit history. Pushing is OPTIONAL — there is no upstream remote we control; this is a local patch repo. If a remote is configured, push.

- [ ] **Step 1: Inspect remotes**

```bash
cd /data2/ruanjianhao/AOrchestra
git remote -v
git log --oneline -10
```

- [ ] **Step 2: Push only if a remote is set**

```bash
cd /data2/ruanjianhao/AOrchestra
git remote | grep -q . && git push || echo "(no remote configured — staying local)"
```

Expected: either a successful push or the explicit message.

### Task 14: Update decision log and progress.md (claw-eval side)

**Files:**
- Modify: `/data2/ruanjianhao/claw-eval/docs/superpowers/specs/aorchestra_decision.md`
- Modify: `/data2/ruanjianhao/claw-eval/docs/progress.md`

**Interfaces:**
- Consumes: numbers from Tasks 10 and 12.
- Produces: durable record that AOrchestra was structurally refactored, with the e2e results that justified it.

- [ ] **Step 1: Append a Phase 5 section to `aorchestra_decision.md`**

Append to `/data2/ruanjianhao/claw-eval/docs/superpowers/specs/aorchestra_decision.md`:

```markdown


---

## Phase 5 — Pluggable SubAgentRuntime (decision log)

**Date:** 2026-06-25
**Spec:** `/data2/ruanjianhao/claw-eval/docs/aopi.md`
**Plan:** `/data2/ruanjianhao/claw-eval/docs/superpowers/plans/2026-06-25-aorchestra-pi-runtime.md`

### What changed in AOrchestra

The hardcoded `DelegateTaskTool → ReActAgent → Runner.run()` dispatch was
replaced with a `SubAgentRuntime` Protocol. Two runtimes are pre-registered:

- `"react"` — `aorchestra/runtime/react_runtime.py` wraps the historical
  ReActAgent+Runner path. No behavioural change.
- `"pi"` — `aorchestra/runtime/pi_runtime.py` spawns a Node worker
  (`aorchestra/runtime/pi_worker/`) that drives `@earendil-works/pi-agent-core`.
  Tool calls round-trip to Python over JSON-RPC stdio; the AO Environment
  services them.

### Why

claw-eval's Wave 4-D e2e baseline against ReActAgent scored 0.28 on T077,
below the 0.3 acceptance bar. Investigation (chat transcript +
`/tmp/pytest-of-root/pytest-88/test_t077_aorchestra_host_e2e0/...`)
showed the SubAgent was hitting Bedrock's "Input too long" because each
attempt re-ingested the full 377K-char OCR output. Rather than patch
the ReAct prompt + memory truncation, we replaced the SubAgent runtime
wholesale with Pi, which has more robust state management and tool-call
semantics.

### What changed in claw-eval

One file: `src/claw_eval/harnesses/aorchestra/_runner.py` now reads
`CLAWEVAL_AORCHESTRA_RUNTIME` (default `"pi"`) and passes it through to
`DelegateTaskTool(runtime_registry=default_registry(), runtime_name=…)`.

### T077 results

- Wave 4-D baseline (`runtime="react"`): **task_score = 0.28**
  - Scores: completion=0.10, robustness=1.00, communication=0.00, safety=1.00
- Phase 5 (`runtime="pi"`): **task_score = <fill in from Task 10>**
  - Scores: <fill in>
- delegate_count under "pi": <fill in>

### Pitfalls explicitly addressed

1. **No loop-in-loop**: PiRuntime never calls `Runner.run()`. Pi owns the
   agent loop; AO Environment is reduced to the tool gateway.
2. **Step budget in the gateway**: `PiRuntime._handle_tool_call` increments
   `steps_taken` and forces `done=True` once `>= spec.max_steps`. Prompts
   carry the budget for the model's planning but are never the enforcement
   mechanism.
3. **Pi built-in tools disabled**: the Node worker sets `noTools: "builtin"`.
   Only AO-Environment-backed tools are reachable.
4. **Sequential tool execution**: worker sets `toolExecution: "sequential"`
   at the Agent level and `executionMode: "sequential"` per tool.
5. **`env.reset()` semantics**: PiRuntime calls `env.reset()` once before the
   first tool call, matching `benchmark/common/runner.py:Runner.run()`.
6. **Trace schema unified**: PiRuntime converts `tool_execution_end` events
   to `StepRecord`-shaped dicts before returning, so
   `DelegateTaskTool._summarize_trace`, claw-eval `_trace_adapter`, and
   AOrchestra trace formatters all consume the same shape.

### Operator notes

- Override runtime: `CLAWEVAL_AORCHESTRA_RUNTIME=react`
- Override node bin: `PI_RUNTIME_NODE_BIN=/path/to/node`
- Override AOrchestra source root: `AORCHESTRA_ROOT=/path/to/AOrchestra`
- Worker must be built: `cd $AORCHESTRA_ROOT/aorchestra/runtime/pi_worker && npm install && npm run build`
```

- [ ] **Step 2: Append a Phase 5 section to `progress.md`**

Append to `/data2/ruanjianhao/claw-eval/docs/progress.md`:

```markdown


---

## Phase 5 — Pluggable SubAgentRuntime + PiRuntime

| Wave | Description | Status |
|---|---|---|
| 5-A | SubAgentRuntime interface + ReActRuntime wrapper + delegate refactor | <fill> |
| 5-B | Node Pi worker + JSON-RPC stdio protocol | <fill> |
| 5-C | PiRuntime Python driver + step-budget gateway | <fill> |
| 5-D | claw-eval default to runtime="pi" + T077 re-run | <fill> |
| 5-E | Regressions, smoke check, decision log | <fill> |

Spec: `docs/aopi.md`
Plan: `docs/superpowers/plans/2026-06-25-aorchestra-pi-runtime.md`
Decision log appended to: `docs/superpowers/specs/aorchestra_decision.md`

### T077 numbers

| Runtime | task_score | completion | robustness | delegate_count |
|---|---|---|---|---|
| react (4-D baseline) | 0.28 | 0.10 | 1.00 | 0 |
| pi (5-D) | <fill> | <fill> | <fill> | <fill> |
```

- [ ] **Step 3: Commit and push the docs (claw-eval side)**

```bash
cd /data2/ruanjianhao/claw-eval
git add docs/superpowers/specs/aorchestra_decision.md docs/progress.md
git commit -m "docs(phase5): runtime split rationale + T077 numbers"
git push origin main 2>&1 | tail -3
```

Expected: push succeeds (claw-eval has a configured remote per Phase 4 history).

---

## Self-Review

**1. Spec coverage:**

- `aopi.md` §"真正应该替换的位置" — Task 4 carves the seam at `DelegateTaskTool → runtime.run()`.
- `aopi.md` §"推荐的整体架构" — Tasks 1 (registry) + 2 (ReActRuntime) + 7 (PiRuntime).
- `aopi.md` §"第一步：给 AO 抽一个 runtime 接口" — Task 1 produces `SubAgentSpec` / `SubAgentRunResult` / Protocol verbatim.
- `aopi.md` §"第二步：把 〈I,C,T,M〉 映射给 Pi" — Task 6's `agent.ts:buildSystemPrompt` + `buildUserPrompt` does the mapping.
- `aopi.md` §"第三步：Pi 工具调用 AO Environment" — Task 6's `tools.ts:buildPythonBridgeTools` round-trips through Python; Task 7's `_handle_tool_call` is the Python side.
- `aopi.md` §"跨语言怎么接" — JSON-RPC stdio (Tasks 5-7).
- `aopi.md` §"Pi 侧建议用 pi-agent-core" — Task 5's `package.json` pins `@earendil-works/pi-agent-core@0.80.2`.
- `aopi.md` §"DelegateTaskTool 最终应该长这样" — Task 4 implements this verbatim with the legacy `runner=` kwarg preserved.
- `aopi.md` §"几个容易踩的大坑" — explicitly addressed in Task 14's decision log; design enforcement lives in:
  - Pitfall 1 (no loop-in-loop): Task 7's `PiRuntime.run` calls the Pi worker, not `Runner.run`.
  - Pitfall 2 (step budget in gateway): Task 7's `_handle_tool_call` enforces it.
  - Pitfall 3 (no Pi built-in tools): Task 6's `agent.ts` sets `noTools: "builtin"`.
  - Pitfall 4 (sequential): `agent.ts` Agent uses `toolExecution: "sequential"` and `tools.ts` builds `executionMode: "sequential"`.
  - Pitfall 5 (env.reset semantics): Task 7's `PiRuntime.run` calls `env.reset()` before the first tool call.
  - Pitfall 6 (trace schema unification): Task 6's `agent.ts` converts Pi events into `StepRecordDict[]` before sending `run_end`, matching `benchmark/common/runner.py:StepRecord` fields.
- `aopi.md` §"最合适的 MVP" — overridden by the user's instruction to go directly to T077; explicitly called out in the Global Constraints.

**2. Placeholder scan:**

- No "TBD"/"TODO"/"implement later" markers in code blocks.
- "fill in" appears in Task 14's decision-log template strings — those are intentional placeholders for **operator-provided** numbers from Task 10/12 (the user runs the e2e and pastes in the actual values), not gaps in the plan itself.
- Every code block is runnable Python/TypeScript/JSON/bash with full content.
- `pi_runtime._build_descriptors` carries an inline TODO comment noting that future revisions could pass full schemas; this is intentional product debt, not a plan gap — it's flagged so it doesn't surprise reviewers.

**3. Type consistency:**

- `SubAgentSpec(instruction: str, context: str, tools: list[str], model: str, original_question: str = "", benchmark_type: str = "terminalbench", max_steps: int = 30, metadata: dict[str, Any] = field(default_factory=dict))` — consistent across Tasks 1, 2, 4, 7, 12, and the worker's `RunStart.spec`.
- `SubAgentRunResult(status, done, steps, finish_result, trace, cost, input_tokens, output_tokens, error)` — consistent across Tasks 1, 2, 4, 7, and the worker's `RunEnd`.
- `RuntimeRegistry.{register, get, names}` — consistent across Tasks 1, 3, 4, 7.
- StepRecord-shaped dict keys (`observation, action, reward, raw_response, done, info`) — consistent across `agent.ts:trace.push`, `PiRuntime` consumer, and `benchmark/common/runner.py:StepRecord`.
- JSON-RPC message types `run_start`, `tool_call`, `tool_result`, `log`, `run_end` and their field names — identical in `protocol.ts` and `pi_runtime.py`.
- `runtime_name` / `runtime_registry` parameter names — identical in Task 4 (`DelegateTaskTool`) and Task 9 (claw-eval `_runner.py`).
- `AORCHESTRA_ROOT`, `PI_RUNTIME_NODE_BIN`, `CLAWEVAL_AORCHESTRA_RUNTIME` env var spellings — consistent across Global Constraints, Tasks 1, 7, 9.

No issues found.
