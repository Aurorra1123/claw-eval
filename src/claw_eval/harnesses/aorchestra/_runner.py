"""AOrchestra MainAgent + SubAgent driver for claw-eval tasks.

Phase 4 Wave 4-D — see docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md
§3 (data flow) and docs/superpowers/plans/2026-06-24-aorchestra-harness.md Task 7.

Public entry: ``await run_one_task(task, env, cfg, *, case_dir, sandbox_url=None)``
returns ``{"trajectory_path": Path, "status": "ok"|"error"|"timeout", "duration_ms": int}``.

Architecture
------------

This module wires together AOrchestra's MainAgent + DelegateTaskTool + CompleteTool
on top of our own ``ClawEvalEnv``. The MainAgent's orchestration loop is a
hand-rolled mirror of ``aorchestra/runners/gaia_runner.py:run_levels`` (steps
204-222) — we cannot reuse ``GAIARunner`` directly because it expects a
GAIA-specific benchmark / dataset / env triple.

``ClawEvalSubAgentRunner`` is the minimal stub that ``DelegateTaskTool``'s
``self.runner.run(sub_agent, env)`` call expects (delegate.py:166). It builds a
small wrapper env that exposes the SubAgent's tool inventory (filtered NOT to
include DelegateTaskTool, to avoid infinite recursion) and dispatches the
SubAgent's chosen action to the matching ``BaseAction``.

Trajectory output schema is a strict subset of ``gaia_runner._save_trajectory``
fields (the Wave 4-C trace adapter consumes the same shape).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Tuple

# AOrchestra is not pip-installable. Inject its source root on sys.path before
# any aorchestra/base import. See docs/superpowers/specs/aorchestra_decision.md §1.
_AORCHESTRA_ROOT = os.environ.get(
    "AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra"
)
if _AORCHESTRA_ROOT not in sys.path:
    sys.path.insert(0, _AORCHESTRA_ROOT)

from base.agent.base_action import BaseAction  # noqa: E402
from base.engine.async_llm import LLMsConfig, create_llm_instance  # noqa: E402
from benchmark.common.env import BasicInfo  # noqa: E402
from benchmark.common.runner import LevelResult, Runner, StepRecord  # noqa: E402

from aorchestra.main_agent import MainAgent  # noqa: E402
from aorchestra.runtime import default_registry  # noqa: E402
from aorchestra.tools.complete import CompleteTool  # noqa: E402
from aorchestra.tools.delegate import DelegateTaskTool  # noqa: E402

if TYPE_CHECKING:
    from ...config import Config
    from ...models.task import TaskDefinition
    from ._bridge.env import ClawEvalEnv


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SubAgent-facing env adapter + runner
# ---------------------------------------------------------------------------


class _SubAgentEnv:
    """Lightweight env wrapper that the standard AOrchestra ``Runner`` can drive.

    Holds the SubAgent's tool inventory (no DelegateTaskTool — second line of
    defence against infinite delegation) and dispatches the SubAgent's chosen
    action to the matching ``BaseAction``. Mirrors the shape of
    ``GAIAOrchestraEnvironment`` (benchmark/aorchestra_bench_gaia.py:46+)
    just enough that ``Runner.run`` can loop.

    The env exposes ``instruction`` as a mutable attribute because
    ``DelegateTaskTool.__call__`` swaps it in for the SubAgent's task
    instruction (delegate.py:160-164).
    """

    def __init__(
        self,
        *,
        instruction: str,
        meta_data: Dict[str, Any],
        max_steps: int,
        tools: List[BaseAction],
    ) -> None:
        self.instruction = instruction
        self.meta_data = meta_data
        self.max_steps = max_steps
        # Index tools by name for O(1) dispatch.
        self._tools: Dict[str, BaseAction] = {t.name: t for t in tools}
        self._steps = 0
        self._done = False

    def _build_action_space(self) -> str:
        """Render tools as a Markdown block the SubAgent prompt expects."""
        if not self._tools:
            return "No tools available."
        chunks: List[str] = []
        for name, tool in self._tools.items():
            chunk = f"### {name}\nDescription: {tool.description}"
            if tool.parameters:
                chunk += f"\nParameters: {json.dumps(tool.parameters, indent=2)}"
            chunks.append(chunk)
        return "Available actions:\n\n" + "\n\n".join(chunks)

    def get_basic_info(self) -> BasicInfo:
        return BasicInfo(
            env_id="claweval-subagent",
            instruction=self.instruction,
            action_space=self._build_action_space(),
            max_steps=self.max_steps,
            meta_data=self.meta_data,
        )

    async def reset(self, seed: int | None = None) -> Dict[str, Any]:
        self._done = False
        self._steps = 0
        return {
            "message": "Environment ready. Use the available tools to complete the task.",
            "current_step": 0,
            "max_steps": self.max_steps,
        }

    async def step(self, action: Dict[str, Any]) -> Tuple[Dict[str, Any], float, bool, Dict[str, Any]]:
        if self._done:
            raise RuntimeError("Environment already finished. Call reset() first.")

        self._steps += 1
        action_type = action.get("action", "")
        params = action.get("params", {}) or {}

        # SubAgent's "finish" action — report progress back to MainAgent
        # without scoring (mirrors GAIAOrchestraEnvironment._handle_finish).
        if action_type == "finish":
            result = params.get("result", "")
            status = params.get("status", "done")
            summary = params.get("summary", "")
            self._done = True
            finish_result = {"result": result, "status": status, "summary": summary}
            return (
                {
                    "message": "Result reported to MainAgent.",
                    "current_step": self._steps,
                    "finish_result": finish_result,
                },
                0.0,
                True,
                {"finished": True, "finish_result": finish_result},
            )

        # Dispatch to a registered tool.
        tool = self._tools.get(action_type)
        if tool is None:
            obs = {
                "error": (
                    f"Unknown action: {action_type}. "
                    f"Available actions: {list(self._tools.keys()) + ['finish']}"
                ),
                "current_step": self._steps,
                "max_steps": self.max_steps,
            }
            if self._steps >= self.max_steps:
                return self._timeout(obs, {"error": "unknown_action"})
            return obs, 0.0, False, {"error": "unknown_action"}

        try:
            result = await tool(**params)
            obs = {
                "action": action_type,
                "result": result,
                "current_step": self._steps,
                "max_steps": self.max_steps,
            }
        except Exception as exc:  # noqa: BLE001
            obs = {
                "action": action_type,
                "success": False,
                "error": str(exc),
                "current_step": self._steps,
                "max_steps": self.max_steps,
            }

        if self._steps >= self.max_steps:
            return self._timeout(obs, {})
        return obs, 0.0, False, {"last_action_result": obs}

    def _timeout(
        self, obs: Dict[str, Any], extra: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], float, bool, Dict[str, Any]]:
        self._done = True
        finish_result = {
            "result": "",
            "status": "timeout",
            "summary": f"Used all {self.max_steps} steps without finish",
        }
        obs["message"] = "Max steps reached"
        obs["finish_result"] = finish_result
        return obs, 0.0, True, {
            **extra,
            "max_steps_reached": True,
            "finished": True,
            "finish_result": finish_result,
        }

    async def close(self) -> None:
        return None


class ClawEvalSubAgentRunner:
    """Minimal runner stub for DelegateTaskTool.

    ``DelegateTaskTool.__call__`` does ``result = await self.runner.run(sub_agent, self.env)``
    (delegate.py:166). ``self.env`` is our ``ClawEvalEnv``; we build a fresh
    ``_SubAgentEnv`` (with the SubAgent tool inventory we were configured with)
    each time ``run`` is invoked so multiple delegations don't share state.

    The standard ``Runner`` (benchmark/common/runner.py) does all the actual
    work — agent.reset / env.reset / loop / record StepRecords.
    """

    def __init__(
        self,
        *,
        sub_tools: List[BaseAction],
        max_steps: int,
        step_timeout: float | None = 600.0,
    ) -> None:
        self._sub_tools = sub_tools
        self._max_steps = max_steps
        self._step_timeout = step_timeout

    async def run(self, sub_agent: Any, clawenv: Any) -> LevelResult:
        # Pull the task instruction from clawenv (DelegateTaskTool may have
        # temporarily overwritten it for the SubAgent).
        instruction = getattr(clawenv, "instruction", "") or ""
        meta_data = getattr(clawenv, "meta_data", {}) or {}

        sub_env = _SubAgentEnv(
            instruction=instruction,
            meta_data=meta_data,
            max_steps=self._max_steps,
            tools=self._sub_tools,
        )

        runner = Runner()
        if self._step_timeout is not None:
            runner.step_timeout = self._step_timeout
        return await runner.run(sub_agent, sub_env)


# ---------------------------------------------------------------------------
# Tool schema builder (Phase 5 — feeds PiRuntime's LLM-facing descriptors)
# ---------------------------------------------------------------------------


def _build_aorchestra_tool_schemas(env: "ClawEvalEnv") -> Dict[str, Dict[str, Any]]:
    """Return per-tool JSON Schemas keyed by action name.

    ``PiRuntime._build_descriptors`` reads ``SubAgentSpec.tool_schemas[name]``
    when present and falls back to a permissive shape otherwise.

    TODO(phase-5-followup): ``ClawEvalEnv``'s action_space is text-only as of
    Phase 4, so we return ``{}`` and let PiRuntime synthesize permissive
    descriptors. A follow-up should hand-build (or auto-derive) JSON Schemas
    for each claw-eval action once we have an action-space inspection helper.
    """
    return {}


# ---------------------------------------------------------------------------
# Trajectory persistence (mirrors gaia_runner._save_trajectory)
# ---------------------------------------------------------------------------


def _save_trajectory(
    *,
    case_dir: Path,
    task: "TaskDefinition",
    main_model: str,
    sub_models: List[str],
    timestamp: str,
    start_time: str,
    end_time: str,
    attempts_detail: List[Dict[str, Any]],
    success: bool,
    total_reward: float,
    total_cost: float,
    main_cost: float,
    sub_cost: float,
    final_answer: str | None,
    error: str | None,
    max_attempts: int,
) -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{task.task_id}_{timestamp}.json"
    out = case_dir / filename
    data = {
        "task_id": task.task_id,
        "timestamp": timestamp,
        "start_time": start_time,
        "end_time": end_time,
        "level": None,
        "question": task.prompt.text,
        "expected_answer": None,
        "file_name": None,
        "main_model": main_model,
        "sub_models": sub_models,
        "max_attempts": max_attempts,
        "success": success,
        "total_reward": total_reward,
        "total_cost": total_cost,
        "main_cost": main_cost,
        "sub_cost": sub_cost,
        "attempts": len(attempts_detail),
        "trajectory": attempts_detail,
        "final_sub_model": sub_models[0] if sub_models else None,
        "error": error,
        "final_answer": final_answer,
        "instruction": task.prompt.text,
        "meta": {},
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_one_task(
    task: "TaskDefinition",
    env: "ClawEvalEnv",
    cfg: "Config",
    *,
    case_dir: Path,
    sandbox_url: str | None = None,
) -> Dict[str, Any]:
    """Run a single claw-eval task through AOrchestra's MainAgent.

    Returns ``{"trajectory_path": Path, "status": "ok"|"error"|"timeout",
    "duration_ms": int}``.

    On exception the function does NOT re-raise — it persists whatever
    trajectory we have so far (possibly empty) and returns ``status="error"``.
    The caller (harness ``_run_host_smoke``) then proceeds to the trace adapter
    which knows how to degrade gracefully (§4.5 / §4.6).
    """
    model_id = cfg.model.model_id
    main_model = model_id
    sub_models: List[str] = [model_id]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    start_time = datetime.now().isoformat()
    started = time.monotonic()

    # Bound the orchestration loop. claw-eval tasks ship ``environment.max_turns``;
    # AOrchestra speaks ``max_attempts``. Fall back to a sane default for tasks
    # that don't set it.
    max_attempts = int(getattr(task.environment, "max_turns", 0) or 10)
    timeout_s = float(getattr(task.environment, "timeout_seconds", 0) or 300)

    # Attach the task instruction onto the env so DelegateTaskTool /
    # _SubAgentEnv can read it (delegate.py:132 reads ``env.instruction``).
    env.instruction = task.prompt.text  # type: ignore[attr-defined]
    env.meta_data = {}  # type: ignore[attr-defined]

    # Build tool sets per §3 / §6 spec:
    #   MainAgent tools = env.get_action_space_for("main") + [DelegateTaskTool, CompleteTool]
    #   SubAgent tools  = env.get_action_space_for("sub")  + [CompleteTool]  (no DelegateTaskTool)
    main_claweval_tools = env.get_action_space_for("main")
    sub_claweval_tools = env.get_action_space_for("sub")

    # Sub-agent runner. Picks the SubAgent's max_steps from the task; the cap
    # is intentionally generous (we want SubAgents to converge before MainAgent
    # decides whether to redelegate).
    sub_max_steps = max(5, int(getattr(task.environment, "max_turns", 0) or 30))
    sub_runner = ClawEvalSubAgentRunner(
        sub_tools=[*sub_claweval_tools, CompleteTool()],
        max_steps=sub_max_steps,
        step_timeout=timeout_s,
    )

    # DelegateTaskTool needs the env, runner, models, and benchmark_type.
    # benchmark_type="gaia" because claw-eval tasks are single-shot Q&A in
    # shape; the GAIA prompt builder doesn't assume a docker container.
    #
    # Phase 5: which sub-agent runtime drives the SubAgents? Default stays
    # "react" (Wave 4-D behaviour). Set CLAWEVAL_AORCHESTRA_RUNTIME=pi to opt
    # into the Node-side @earendil-works/pi-agent-core worker. We hold off on
    # flipping the default until the Pi side has cleared T077.
    runtime_name = os.environ.get("CLAWEVAL_AORCHESTRA_RUNTIME", "react")
    delegate_tool = DelegateTaskTool(
        env=env,
        runner=sub_runner,
        models=sub_models,
        benchmark_type="gaia",
        runtime_registry=default_registry(),
        runtime_name=runtime_name,
        tool_schemas=_build_aorchestra_tool_schemas(env),
    )
    complete_tool = CompleteTool()
    main_tools: List[BaseAction] = [
        *main_claweval_tools,
        delegate_tool,
        complete_tool,
    ]

    attempts_detail: List[Dict[str, Any]] = []
    final_answer: str | None = None
    error_msg: str | None = None
    status: Literal["ok", "error", "timeout"] = "ok"
    main_cost_before = 0.0
    main_cost_after = 0.0
    success = False

    try:
        # Build the MainAgent's LLM. patched_llms_config (called by the
        # harness) has already swapped in our claw-eval endpoint.
        main_llm = create_llm_instance(LLMsConfig.default().get(main_model))

        # Construct the MainAgent. GAIA prompt builder is the closest match —
        # claw-eval tasks are Q&A with a final answer.
        from aorchestra.prompts.gaia import GAIAMainAgentPrompt

        main_agent = MainAgent(
            name="MainAgent",
            llm=main_llm,
            sub_models=sub_models,
            tools=main_tools,
            subagent_tools=[*sub_claweval_tools, complete_tool],
            prompt_builder=GAIAMainAgentPrompt,
            max_attempts=max_attempts,
            benchmark_type="gaia",
            mask_model_names=False,  # we're single-model; aliasing adds noise.
        )

        main_info = BasicInfo(
            env_id=task.task_id,
            instruction=task.prompt.text,
            action_space="",
            max_steps=max_attempts,
            meta_data={},
        )
        main_agent.reset(main_info)

        try:
            main_cost_before = main_agent.get_usage_cost()
        except Exception:  # noqa: BLE001 — defensive
            main_cost_before = 0.0

        async def _loop() -> None:
            nonlocal final_answer, success
            for attempt_idx in range(max_attempts):
                action_result, raw_response = await main_agent.step(None, [])
                action_name = action_result.get("action")
                params = action_result.get("params", {}) or {}
                result = action_result.get("result", {})

                attempts_detail.append({
                    "attempt": attempt_idx + 1,
                    "action": action_name,
                    "params": params,
                    "result": result,
                    "raw_response": raw_response,
                })

                if action_name == "complete":
                    ans = params.get("answer") if isinstance(params, dict) else None
                    if ans is not None:
                        final_answer = str(ans)
                    success = True
                    break

        # Outer wall-clock guard. ``Runner.step_timeout`` already bounds each
        # SubAgent step; this is the catch-all so the task can't wedge if the
        # MainAgent itself stalls.
        try:
            await asyncio.wait_for(_loop(), timeout=timeout_s)
        except asyncio.TimeoutError:
            status = "timeout"
            error_msg = f"orchestration timed out after {timeout_s}s"
            _log.warning("[aorchestra] task %s timed out", task.task_id)

        try:
            main_cost_after = main_agent.get_usage_cost()
        except Exception:  # noqa: BLE001
            main_cost_after = main_cost_before

    except Exception as exc:  # noqa: BLE001
        status = "error"
        error_msg = f"{type(exc).__name__}: {exc}"
        _log.exception("[aorchestra] uncaught error driving task %s", task.task_id)
        # Stash traceback in the attempts detail so post-mortem is possible
        # without re-running.
        attempts_detail.append({
            "attempt": len(attempts_detail) + 1,
            "action": "error",
            "params": {},
            "result": {"error": error_msg, "traceback": traceback.format_exc()},
            "raw_response": "",
        })

    end_time = datetime.now().isoformat()
    duration_ms = int((time.monotonic() - started) * 1000)

    # Aggregate per-attempt sub_cost the same way gaia_runner does
    # (lines 250-254): sum each delegate_task result's cost field.
    sub_cost = 0.0
    for a in attempts_detail:
        res = a.get("result") or {}
        if isinstance(res, dict):
            try:
                sub_cost += float(res.get("cost", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass
    main_cost = max(0.0, float(main_cost_after) - float(main_cost_before))
    total_cost = main_cost + sub_cost

    trajectory_path = _save_trajectory(
        case_dir=case_dir,
        task=task,
        main_model=main_model,
        sub_models=sub_models,
        timestamp=timestamp,
        start_time=start_time,
        end_time=end_time,
        attempts_detail=attempts_detail,
        success=success and status == "ok",
        total_reward=1.0 if success else 0.0,
        total_cost=total_cost,
        main_cost=main_cost,
        sub_cost=sub_cost,
        final_answer=final_answer,
        error=error_msg,
        max_attempts=max_attempts,
    )

    return {
        "trajectory_path": trajectory_path,
        "status": status,
        "duration_ms": duration_ms,
    }
