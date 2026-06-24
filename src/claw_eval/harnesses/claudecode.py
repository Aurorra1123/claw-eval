"""ClaudeCodeHarness — placeholder.

Phase 3 §6.8 — see ``docs/harness_design.md``.

Reserves the registry slot and CLI surface for the Claude Code CLI agent.
The real implementation will follow the same pattern as ``OpenClawHarness``:

* a ``_claudecode_native`` runner that drives the Claude Code subprocess and
  parses its session output,
* a bridge mechanism (Claude Code's tool/MCP surface) that routes
  ``task.tool_endpoints`` / ``SANDBOX_TOOL_NAMES`` to claw-eval mock services
  + the in-container sandbox server,
* a ``_trace_adapter.translate_claudecode`` that emits the same claw-eval
  JSONL schema the grader consumes.

Until that lands, both ``preflight`` and ``run`` short-circuit so the CLI
accepts ``--harness claudecode`` as a valid choice but refuses to actually
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


class ClaudeCodeHarness:
    """Claude Code CLI harness — not yet implemented.

    Same placeholder contract as :class:`CodexHarness`: visible to the
    registry / CLI ``--harness`` choices, but every call rejects until the
    real Claude Code driver lands. Fail loud rather than silently producing
    a bogus trace.
    """

    name = "claudecode"
    # Empty feature set advertises "this harness honours no task.yaml field"
    # — true until the real implementation lands. The future implementation
    # will likely mirror OpenClaw's ``frozenset({"http_services", "sandbox_tools"})``.
    supported_features = frozenset()

    def preflight(self, task: "TaskDefinition") -> list[str]:
        """Reject every task with a clear "not implemented" message."""
        return ["claudecode harness is not implemented yet (Phase 3 §6.8 placeholder)"]

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
            "claudecode harness is not implemented yet. See docs/harness_design.md §6.8."
        )
