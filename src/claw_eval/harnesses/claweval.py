"""ClawEvalHarness — zero-behaviour-change wrapper around runner.loop.run_task.

Phase 3 §3.3 — see docs/harness_design.md.

This harness is a thin shim. ``run_task`` and ``_collect_env_snapshot`` are
the existing pieces of code, called as-is. The only purpose is to give every
harness a uniform entry point so cli.py can dispatch by name.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .base import HarnessResult

if TYPE_CHECKING:
    from ..config import Config
    from ..models.task import TaskDefinition
    from ..runner.providers.openai_compat import OpenAICompatProvider
    from ..runner.sandbox_runner import ContainerHandle
    from ..runner.services import ServiceManager
    from ..runner.user_agent import UserAgent


class ClawEvalHarness:
    """Drive a task using the native claw-eval agent loop.

    The behavioural contract is "identical to calling ``run_task`` directly"
    — the trace JSONL and env_snapshot produced through this harness must
    match the pre-refactor output byte-for-byte except for the new
    ``TraceStart.harness`` field (which defaults to ``"claweval"`` on both
    sides, so even that should match in practice).
    """

    name = "claweval"
    supported_features = frozenset({
        "http_services",
        "sandbox_tools",
        "user_agent",
        "compact",
        "max_turns_strict",
    })

    def preflight(self, task: "TaskDefinition") -> list[str]:
        """ClawEval is the reference implementation — no task is rejected."""
        return []

    def _build_provider(self, cfg: "Config") -> "OpenAICompatProvider":
        # Imported lazily to avoid pulling the OpenAI SDK at module import time.
        from ..runner.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            model_id=cfg.model.model_id,
            api_key=cfg.model.api_key,
            base_url=cfg.model.base_url,
            extra_body=cfg.model.extra_body,
            temperature=cfg.model.temperature,
            reasoning_effort=cfg.model.reasoning_effort,
        )

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
        # Lazy import keeps the import graph identical to the pre-harness layout
        # (cli.py used to import run_task at call time).
        from ..runner.loop import run_task

        provider = self._build_provider(cfg)

        # SANDBOX_TOOLS are enabled either by a docker container handle (HTTP
        # dispatch into the sandbox server) or by the local --sandbox-tools
        # flag (local subprocess dispatch). The two are separate but both
        # exposed to run_task through ``sandbox_tools=True``.
        use_sandbox_tools = sandbox_handle is not None or sandbox_tools
        sandbox_url = sandbox_handle.sandbox_url if sandbox_handle is not None else None

        trace_path = run_task(
            task,
            provider,
            trace_dir=trace_dir,
            sandbox_tools=use_sandbox_tools,
            sandbox_url=sandbox_url,
            prompt_cfg=cfg.prompt,
            model_cfg=cfg.model,
            media_cfg=cfg.media,
            user_agent=user_agent,
        )

        # NB: env_snapshot collection is intentionally kept in the CLI rather
        # than baked into this harness — the CLI's sandbox path needs to slip
        # ``inject_grader_files`` between ``run_task`` and the snapshot call so
        # grader-only files (verify scripts with answers) never appear in the
        # agent's view but DO appear in the snapshot. Moving _collect_env_snapshot
        # inside this method would break that ordering and is a regression vs.
        # the pre-Phase-3 behaviour. See docs/harness_design.md §3.3 for the
        # original skeleton; this is the wave-1 deviation, reported in the
        # wave-1 report.
        # Other harnesses (OpenClaw etc.) which produce their own snapshots
        # via _collect_workdir_snapshot still set HarnessResult.env_snapshot —
        # the CLI honours whichever channel is non-None.

        # audit_data deliberately left empty: under the claweval path audit
        # snapshots are written into the trace JSONL by run_task itself and the
        # grader gets them via load_trace(). HarnessResult.audit_data is the
        # alternate channel used by external-CLI harnesses (e.g. OpenClaw) that
        # cannot stream AuditSnapshot events live.
        return HarnessResult(
            trace_path=trace_path,
            env_snapshot=None,
            audit_data={},
            raw_dir=None,
        )
