"""CodexHarness — placeholder.

Phase 3 §6.8 — see ``docs/harness_design.md``.

Reserves the registry slot and CLI surface for the Codex CLI agent
(`@openai/codex`). The real implementation will follow the same pattern as
``OpenClawHarness``:

* a ``_codex_native`` runner that drives the Codex subprocess and parses
  its session output,
* a bridge plugin (or whatever the Codex tooling mechanism turns out to be)
  that routes ``task.tool_endpoints`` / ``SANDBOX_TOOL_NAMES`` to claw-eval
  mock services + the in-container sandbox server,
* a ``_trace_adapter.translate_codex`` that emits the same claw-eval JSONL
  schema the grader consumes.

Until that lands, both ``preflight`` and ``run`` short-circuit so the CLI
accepts ``--harness codex`` as a valid choice but refuses to actually
schedule a task on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .base import HarnessResult

if TYPE_CHECKING:
    from ..config import Config
    from ..models.task import TaskDefinition
    from ..runner.sandbox_runner import ContainerHandle
    from ..runner.services import ServiceManager
    from ..runner.user_agent import UserAgent


class CodexHarness:
    """Codex CLI harness — not yet implemented.

    The contract surface (``name``, ``supported_features``, ``preflight``,
    ``run``) matches every other harness so the registry / CLI / grader
    pipeline accepts this name without special-casing. Calls to ``run`` raise
    ``NotImplementedError`` immediately — anyone trying to actually drive a
    task through Codex will fail loud, not silently produce a bogus trace.
    """

    name = "codex"
    # Empty feature set advertises "this harness honours no task.yaml field"
    # — which is the truthful default until the real implementation lands.
    # When Codex support arrives, this will mirror OpenClaw's
    # ``frozenset({"http_services", "sandbox_tools"})``.
    supported_features = frozenset()

    def preflight(self, task: "TaskDefinition") -> list[str]:
        """Reject every task with a clear "not implemented" message."""
        return ["codex harness is not implemented yet (Phase 3 §6.8 placeholder)"]

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
        raise NotImplementedError(
            "codex harness is not implemented yet. See docs/harness_design.md §6.8."
        )
