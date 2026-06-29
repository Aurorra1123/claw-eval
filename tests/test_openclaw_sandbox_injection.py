"""Tests for OpenClawHarness._inject_sandbox_tools.

Aligns the openclaw harness's container path with the original claw-eval loop
(``runner/loop.py:294-302``), which unconditionally appends the full
SANDBOX_TOOLS set to the task tool list (deduping names already declared in
task.yaml) when running in container/sandbox mode. Without this, a task that
declares no tools (e.g. multimodal M007: ``tools: []``) gets an empty
bridgeable set, the allowlist collapses to scaffolding, and the agent has no
read/write/ReadMedia tool — it cannot read the image or write output.html.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claw_eval.harnesses.openclaw import OpenClawHarness  # noqa: E402
from claw_eval.models.task import TaskDefinition  # noqa: E402
from claw_eval.runner.sandbox_tools import SANDBOX_TOOLS, SANDBOX_TOOL_NAMES  # noqa: E402

_ALL_SANDBOX = {t.name for t in SANDBOX_TOOLS}


def _load(task_id: str) -> TaskDefinition:
    return TaskDefinition.from_yaml(str(REPO_ROOT / "tasks" / task_id / "task.yaml"))


def test_inject_into_empty_tools_adds_full_sandbox_set():
    """A task with no declared tools gets the full 9-tool SANDBOX set."""
    task = _load("M007_score_symphony")
    assert [t.name for t in task.tools] == []  # precondition

    injected = OpenClawHarness._inject_sandbox_tools(task)

    names = {t.name for t in injected.tools}
    assert names == _ALL_SANDBOX
    assert names == set(SANDBOX_TOOL_NAMES)


def test_inject_does_not_mutate_original_task():
    """Injection returns a copy; the original task's tools stay unchanged."""
    task = _load("M007_score_symphony")
    before = list(task.tools)

    OpenClawHarness._inject_sandbox_tools(task)

    assert list(task.tools) == before  # original untouched
    assert [t.name for t in task.tools] == []


def test_inject_dedupes_names_already_declared():
    """A SANDBOX_TOOL name already in task.tools is not duplicated; the
    task's own spec is kept (mirrors loop.py:300-301 dedupe by name)."""
    task = _load("M007_score_symphony")
    # Synthesise a task that already declares one sandbox tool (Read) with a
    # custom spec, by copying an existing SANDBOX_TOOLS spec for Read.
    read_spec = next(t for t in SANDBOX_TOOLS if t.name == "Read")
    task_with_read = task.model_copy(update={"tools": [read_spec]})

    injected = OpenClawHarness._inject_sandbox_tools(task_with_read)

    names = [t.name for t in injected.tools]
    # Every sandbox tool present, exactly once (Read not duplicated).
    assert sorted(names) == sorted(_ALL_SANDBOX)
    assert names.count("Read") == 1


def test_inject_preserves_existing_non_sandbox_tools():
    """Bridge/mock tools declared by the task survive injection alongside the
    sandbox set (general tasks keep their mock tools)."""
    task = _load("M007_score_symphony")
    read_spec = next(t for t in SANDBOX_TOOLS if t.name == "Read")
    # Fake a mock tool spec by reusing a sandbox spec under a non-sandbox name.
    mock_like = read_spec.model_copy(update={"name": "gmail_list_messages"})
    task_mixed = task.model_copy(update={"tools": [mock_like]})

    injected = OpenClawHarness._inject_sandbox_tools(task_mixed)

    names = {t.name for t in injected.tools}
    assert "gmail_list_messages" in names
    assert _ALL_SANDBOX <= names
