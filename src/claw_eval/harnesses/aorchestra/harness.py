"""AOrchestraHarness — drive claw-eval tasks through MainAgent + SubAgent.

Phase 4 Wave 4-D — see ``docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md``
§3 (data flow), §4 (errors), §6 (Wave 4-D specifics).

Two execution modes (mirroring the OpenClaw split, but asymmetric):

* **host smoke** (Wave 4-D) — ``sandbox_handle is None``. AOrchestra runs as a
  Python in-process library on host; valid for tasks WITHOUT
  SANDBOX_TOOL_NAMES (no Bash/Read/Write/...). The CLI gate refuses the
  sandbox-tool case at the entry point before reaching the harness.
* **container** (Wave 4-E) — ``sandbox_handle`` provided. Same as host smoke
  but SANDBOX_TOOL_NAMES bridge to the in-container sandbox server. Not yet
  implemented; the stub raises ``NotImplementedError``.

§4.2 asymmetry vs OpenClaw: AOrchestra is OK in host mode when the task has
no SANDBOX_TOOLS, because it's a Python library (no subprocess to isolate).
OpenClaw is always docker because the OpenClaw subprocess can read host files
directly.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# AOrchestra is not pip-installable. Inject its source root on sys.path before
# any aorchestra/base import (the bridge already does this, but harness.py is a
# valid entry point too). See docs/superpowers/specs/aorchestra_decision.md §1.
_AORCHESTRA_ROOT = os.environ.get(
    "AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra"
)
if _AORCHESTRA_ROOT not in sys.path:
    sys.path.insert(0, _AORCHESTRA_ROOT)

from ...runner.sandbox_tools import SANDBOX_TOOL_NAMES
from .._snapshot import collect_workdir_snapshot, inject_grader_files_host
from ..base import HarnessResult
from ._bridge import ClawEvalEnv, patched_llms_config
from ._trace_adapter import translate_aorchestra

if TYPE_CHECKING:
    from ...config import Config
    from ...models.task import TaskDefinition
    from ...runner.sandbox_runner import ContainerHandle
    from ...runner.services import ServiceManager
    from ...runner.user_agent import UserAgent


_log = logging.getLogger(__name__)


class AOrchestraHarness:
    """First-class claw-eval harness backed by AOrchestra's MainAgent + SubAgent."""

    name = "aorchestra"
    supported_features = frozenset({"http_services", "sandbox_tools"})

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def preflight(self, task: "TaskDefinition") -> list[str]:
        """Reject tasks whose semantics AOrchestra can't honour (§4.1).

        Strict rejections:

        - ``task.user_agent.enabled``: AOrchestra is a one-shot orchestration
          loop; it has no mid-run "simulated user" injection hook.

        Deferred rejections (not done here):

        - SANDBOX_TOOL_NAMES without ``sandbox_handle``: the CLI gate refuses
          this at entry. The harness's ``_run_host_smoke`` adds a second line
          of defence by raising ``SystemExit(2)`` if it slips through.
        - JSON Schema constructs (oneOf/allOf/$ref): deferred to the bridge,
          which raises ``SchemaTranslationError`` when it can't compile a
          tool. Catching it here would require importing the bridge for
          every preflight — too expensive.
        """
        errs: list[str] = []
        ua = getattr(task, "user_agent", None)
        if ua is not None and getattr(ua, "enabled", False):
            errs.append("aorchestra harness does not support simulated user_agent")
        return errs

    # ------------------------------------------------------------------
    # Run — dispatcher
    # ------------------------------------------------------------------

    def run(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        cfg: "Config",
        sandbox_handle: "ContainerHandle | None",
        user_agent: "UserAgent | None",
        services_ctx: "ServiceManager | None",
        sandbox_tools: bool = False,
    ) -> HarnessResult:
        """Dispatch to host smoke or container path.

        ``user_agent`` is accepted to keep the Protocol signature uniform but
        ignored — preflight already rejects tasks that need it.
        """
        if sandbox_handle is not None:
            return self._run_container(
                task,
                trace_dir=trace_dir,
                run_id=run_id,
                cfg=cfg,
                sandbox_handle=sandbox_handle,
                services_ctx=services_ctx,
            )
        return self._run_host_smoke(
            task,
            trace_dir=trace_dir,
            run_id=run_id,
            cfg=cfg,
            services_ctx=services_ctx,
        )

    # ------------------------------------------------------------------
    # Host smoke path (Wave 4-D)
    # ------------------------------------------------------------------

    def _run_host_smoke(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        cfg: "Config",
        services_ctx: "ServiceManager | None",
    ) -> HarnessResult:
        """Host smoke path: AOrchestra in-process, no sandbox container.

        Only valid for tasks WITHOUT SANDBOX_TOOL_NAMES. The CLI gate
        ``cmd_run`` / ``cmd_run_inner`` / ``cmd_batch`` already enforces this,
        but we add a second check here per §4.2: defence in depth.
        """
        import asyncio

        # ---- 0. Second-line gate: refuse sandbox tools in host mode ----
        sandbox_tool_names = [t.name for t in task.tools if t.name in SANDBOX_TOOL_NAMES]
        if sandbox_tool_names:
            print(
                "ERROR: AOrchestra host mode cannot run tasks that declare "
                f"sandbox tools {sandbox_tool_names}. Pass --sandbox to use the "
                "container path (Wave 4-E).",
                file=sys.stderr,
            )
            raise SystemExit(2)

        trace_dir = Path(trace_dir)
        trace_dir.mkdir(parents=True, exist_ok=True)
        task_dir = self._task_dir(task)

        case_dir = trace_dir / f"{task.task_id}_{run_id}_raw"
        case_dir.mkdir(parents=True, exist_ok=True)

        # ---- 1-2. Patch LLMsConfig + open ClawEvalEnv + run MainAgent ----
        # Lazy import so the heavy AOrchestra-LLM stack only loads when the
        # harness is actually invoked.
        from . import _runner

        with patched_llms_config(cfg.model):
            with ClawEvalEnv(task, sandbox_url=None) as env:
                raw = asyncio.run(
                    _runner.run_one_task(
                        task,
                        env,
                        cfg,
                        case_dir=case_dir,
                        sandbox_url=None,
                    )
                )
                # Persist the bridge step_log so the trace adapter can read it.
                # We snapshot inside the env's lifetime to capture every record.
                step_log_path = case_dir / "step_log.jsonl"
                self._write_step_log(env, step_log_path)

        # ---- 3. Audit data from mock services (still alive on host) ----
        audit_data = self._collect_audit(task, services_ctx)

        # ---- 4. env_snapshot — only when the task asks for one ----
        env_snapshot = None
        if (
            getattr(task, "sandbox_grader_files", None)
            or getattr(task, "env_snapshot_files", None)
            or getattr(task, "env_snapshot_commands", None)
        ):
            # In host smoke mode we don't have a separate workdir per the
            # OpenClaw pattern — the agent runs in the same process, so
            # ``task_dir`` IS the workdir for snapshot purposes (the host
            # already has the task's fixture files). This matches the spec
            # §4.3 row "no SANDBOX_TOOLS but has env_snapshot_*".
            work_dir = task_dir
            inject_grader_files_host(task, work_dir, task_dir=task_dir)
            env_snapshot = collect_workdir_snapshot(
                work_dir, task, task_dir=task_dir
            )

        # ---- 5. Translate trajectory + step_log + audit into trace JSONL ----
        trace_path = translate_aorchestra(
            trajectory_path=raw["trajectory_path"],
            step_log_path=step_log_path,
            audit_data=audit_data,
            task=task,
            run_id=run_id,
            trace_dir=trace_dir,
            duration_ms=raw["duration_ms"],
            status=raw["status"],
        )

        return HarnessResult(
            trace_path=trace_path,
            env_snapshot=env_snapshot,
            audit_data=audit_data,
            raw_dir=case_dir,
        )

    # ------------------------------------------------------------------
    # Container path (Wave 4-E stub)
    # ------------------------------------------------------------------

    def _run_container(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        cfg: "Config",
        sandbox_handle: "ContainerHandle",
        services_ctx: "ServiceManager | None",
    ) -> HarnessResult:
        """Container path — implemented in Wave 4-E (Task 8)."""
        raise NotImplementedError(
            "Wave 4-E will implement the container path "
            "(SANDBOX_TOOL_NAMES bridge to the sandbox server)."
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _task_dir(task: "TaskDefinition") -> Path:
        return (
            Path(task.task_file).parent
            if getattr(task, "task_file", None)
            else Path.cwd()
        )

    @staticmethod
    def _write_step_log(env: "ClawEvalEnv", path: Path) -> None:
        """Dump ``env.step_log()`` as JSONL for the trace adapter to consume."""
        import json as _json

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for rec in env.step_log():
                fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def _collect_audit(
        task: "TaskDefinition",
        services_ctx: "ServiceManager | None",
    ) -> dict[str, dict]:
        """Pull ``/audit`` from every mock service declared by ``task``.

        Verbatim from OpenClawHarness._collect_audit — we derive the audit URL
        from each service's ``reset_endpoint`` by swapping ``/reset`` for
        ``/audit``. ``services_ctx`` is accepted only to keep the signature
        symmetric with claweval; we don't use it.
        """
        audit: dict[str, dict] = {}
        if not getattr(task, "services", None):
            return audit
        import httpx

        for svc in task.services:
            if not getattr(svc, "reset_endpoint", None):
                continue
            audit_url = svc.reset_endpoint.rsplit("/reset", 1)[0] + "/audit"
            try:
                resp = httpx.get(audit_url, timeout=5)
                audit[svc.name] = (
                    resp.json()
                    if resp.status_code == 200
                    else {"error": f"audit fetch failed: HTTP {resp.status_code}"}
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning("audit fetch failed for service %s: %s", svc.name, exc)
                audit[svc.name] = {"error": str(exc)}
        return audit
