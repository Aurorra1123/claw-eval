"""ClawEvalEnv — adapter that lets AOrchestra agents drive a claw-eval task.

Owns the per-run step_log list, knows about the task's tool inventory, and
hands out BaseAction instances tagged with the requesting agent role.

Lifecycle:
  with ClawEvalEnv(task, sandbox_url=...) as env:
      obs = env.reset()
      actions = env.get_action_space_for("main")
      # ... agent runs ...
      log = env.step_log()

Phase 4 Wave 4-B Task 4 — see docs/superpowers/plans/2026-06-24-aorchestra-harness.md.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Literal

# AOrchestra is not pip-installable. Inject its source root on sys.path before
# the first BaseAction import. See docs/superpowers/specs/aorchestra_decision.md §1.
_AORCHESTRA_ROOT = os.environ.get(
    "AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra"
)
if _AORCHESTRA_ROOT not in sys.path:
    sys.path.insert(0, _AORCHESTRA_ROOT)

from base.agent.base_action import BaseAction  # noqa: E402

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
        # httpx.AsyncClient is owned per-call inside _post (actions.py) so
        # there's nothing global to close here. The flag is purely a sanity
        # check against any future use-after-close.

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
