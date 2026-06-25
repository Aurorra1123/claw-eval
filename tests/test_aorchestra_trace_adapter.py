"""Tests for the AOrchestra → claw-eval trace translator.

Phase 4 Wave 4-C — see ``docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md``
§5.2 (test coverage) and ``docs/superpowers/plans/2026-06-24-aorchestra-harness.md``
Task 6.

Fixtures live under ``tests/fixtures/aorchestra/``:

- ``trajectory_sample.json`` — handcrafted, mirrors the schema written by
  ``aorchestra/runners/gaia_runner.py::_save_trajectory``. Contains 2
  MainAgent tool steps (ocr_extract_text + delegate_task) + 1 SubAgent
  sub-attempt (ocr_extract_text with status=500) + 1 SubAgent complete +
  1 MainAgent final complete.
- ``step_log_sample.jsonl`` — handcrafted, two records aligned with the
  trajectory's toolCallIds (``abc123`` status=200 / ``def456`` status=500).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claw_eval.graders.base import AbstractGrader
from claw_eval.harnesses.aorchestra._trace_adapter import translate_aorchestra
from claw_eval.models.content import ToolResultBlock, ToolUseBlock
from claw_eval.models.task import Environment, Prompt, TaskDefinition
from claw_eval.models.trace import DimensionScores, MediaLoad, ToolDispatch, TraceMessage
from claw_eval.trace.reader import load_trace

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "aorchestra"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trajectory_path() -> Path:
    return FIXTURES_DIR / "trajectory_sample.json"


@pytest.fixture
def step_log_path() -> Path:
    return FIXTURES_DIR / "step_log_sample.jsonl"


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        task_id="T077_officeqa_highest_dept_spending",
        task_name="OfficeQA highest-spending department",
        prompt=Prompt(
            text=(
                "Which department had the highest spending last month? "
                "Use the OCR tool to read the budget PDF and report the answer."
            ),
            language="en",
        ),
        environment=Environment(timeout_seconds=300, max_turns=20),
    )


@pytest.fixture
def audit_data() -> dict[str, dict]:
    return {
        "ocr": {
            "calls": [
                {
                    "endpoint": "/ocr/extract",
                    "request_body": {"image_path": "/data/budget.pdf"},
                    "response_body": {"status": 200},
                    "status": 200,
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# 1. Basic translation produces the expected event mix
# ---------------------------------------------------------------------------


def test_translate_basic(
    tmp_path,
    trajectory_path,
    step_log_path,
    task,
    audit_data,
):
    trace_path = translate_aorchestra(
        trajectory_path=trajectory_path,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="run0",
        trace_dir=tmp_path,
        duration_ms=4_200,
        status="ok",
    )

    assert trace_path.exists()
    assert trace_path.name == "T077_officeqa_highest_dept_spending_run0.jsonl"

    with trace_path.open() as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    types = [ev["type"] for ev in lines]

    # TraceStart at head, TraceEnd at tail.
    assert types[0] == "trace_start"
    assert types[-1] == "trace_end"
    assert lines[0]["harness"] == "aorchestra"
    assert lines[0]["task_id"] == task.task_id
    assert lines[0]["model"] == "claude-sonnet-4-5"

    n_start = sum(1 for t in types if t == "trace_start")
    n_message = sum(1 for t in types if t == "message")
    n_dispatch = sum(1 for t in types if t == "tool_dispatch")
    n_audit = sum(1 for t in types if t == "audit_snapshot")
    n_end = sum(1 for t in types if t == "trace_end")

    assert n_start == 1
    # Expected ≥3 messages: 1 user prompt + at least 2 assistant entries.
    assert n_message >= 3
    # Expected ≥2 ToolDispatch (one per step_log record).
    assert n_dispatch >= 2
    assert n_audit >= 0
    assert n_audit == 1  # ocr service
    assert n_end == 1

    end = lines[-1]
    assert end["failure_modes"] == []


# ---------------------------------------------------------------------------
# 2. agent_role gets filled with main + sub
# ---------------------------------------------------------------------------


def test_agent_role_filled(
    tmp_path,
    trajectory_path,
    step_log_path,
    task,
    audit_data,
):
    trace_path = translate_aorchestra(
        trajectory_path=trajectory_path,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="r1",
        trace_dir=tmp_path,
        duration_ms=1_000,
        status="ok",
    )

    start, messages, dispatches, _, end, _ = load_trace(trace_path)

    roles_in_messages = {m.agent_role for m in messages}
    roles_in_dispatches = {d.agent_role for d in dispatches}

    assert "main" in roles_in_messages
    assert "sub" in roles_in_messages, (
        f"expected at least one 'sub' agent_role on messages; got {roles_in_messages}"
    )
    # The step_log fixture has both main and sub records, so both should
    # propagate into ToolDispatch events.
    assert "main" in roles_in_dispatches
    assert "sub" in roles_in_dispatches


# ---------------------------------------------------------------------------
# 3. load_trace roundtrip returns the canonical six-tuple
# ---------------------------------------------------------------------------


def test_load_trace_roundtrip(
    tmp_path,
    trajectory_path,
    step_log_path,
    task,
    audit_data,
):
    trace_path = translate_aorchestra(
        trajectory_path=trajectory_path,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="r2",
        trace_dir=tmp_path,
        duration_ms=2_500,
        status="ok",
    )

    start, messages, dispatches, media_events, end, audit_out = load_trace(trace_path)

    assert start.harness == "aorchestra"
    assert start.task_id == task.task_id
    assert start.model == "claude-sonnet-4-5"

    assert len(messages) >= 3
    assert len(dispatches) >= 2
    assert media_events == []
    assert end is not None
    assert "ocr" in audit_out
    assert audit_out["ocr"]["calls"][0]["status"] == 200

    # The first message is the user prompt.
    assert messages[0].message.role == "user"
    assert messages[0].agent_role == "main"

    # At least one assistant message should carry a ToolUseBlock.
    tool_uses = [
        b
        for m in messages
        if m.message.role == "assistant"
        for b in m.message.content
        if isinstance(b, ToolUseBlock)
    ]
    assert len(tool_uses) >= 1
    assert any(tu.id == "abc123" for tu in tool_uses)


# ---------------------------------------------------------------------------
# 4. A minimal AbstractGrader subclass can consume the trace without error
# ---------------------------------------------------------------------------


class _NoopGrader(AbstractGrader):
    """Minimal grader that returns default DimensionScores."""

    def grade(
        self,
        messages: list[TraceMessage],
        dispatches: list[ToolDispatch],
        task: TaskDefinition,
        audit_data: dict[str, dict] | None = None,
        judge: Any | None = None,
        media_events: list[MediaLoad] | None = None,
        env_snapshot: dict | None = None,
    ) -> DimensionScores:
        # Touch every input so we exercise the trace shape, but return a
        # default DimensionScores. Any TypeError / AttributeError surfaces.
        _ = AbstractGrader._get_final_assistant_text(messages)
        _ = AbstractGrader.compute_robustness(dispatches)
        _ = AbstractGrader.format_conversation(messages)
        return DimensionScores()


def test_grader_can_consume(
    tmp_path,
    trajectory_path,
    step_log_path,
    task,
    audit_data,
):
    trace_path = translate_aorchestra(
        trajectory_path=trajectory_path,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="r3",
        trace_dir=tmp_path,
        duration_ms=1_000,
        status="ok",
    )

    start, messages, dispatches, media_events, end, audit_out = load_trace(trace_path)
    grader = _NoopGrader()
    scores = grader.grade(
        messages=messages,
        dispatches=dispatches,
        task=task,
        audit_data=audit_out,
        judge=None,
        media_events=media_events,
        env_snapshot=None,
    )
    assert isinstance(scores, DimensionScores)


# ---------------------------------------------------------------------------
# 5. trajectory_path=None → minimal trace + failure_modes=["error"]
# ---------------------------------------------------------------------------


def test_partial_trajectory_missing(
    tmp_path,
    step_log_path,
    task,
    audit_data,
):
    trace_path = translate_aorchestra(
        trajectory_path=None,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="r4",
        trace_dir=tmp_path,
        duration_ms=500,
        status="error",
    )

    start, messages, dispatches, media_events, end, audit_out = load_trace(trace_path)

    # Minimal trace: TraceStart + user prompt + (optional dispatches/audit) + TraceEnd
    assert start.harness == "aorchestra"
    assert len(messages) == 1
    assert messages[0].message.role == "user"
    assert messages[0].message.text == task.prompt.text
    assert end is not None
    assert "error" in end.failure_modes


# ---------------------------------------------------------------------------
# 6. trajectory file containing ``{}`` → same minimal trace
# ---------------------------------------------------------------------------


def test_partial_trajectory_empty_file(
    tmp_path,
    step_log_path,
    task,
    audit_data,
):
    empty_path = tmp_path / "empty_trajectory.json"
    empty_path.write_text("{}")

    trace_path = translate_aorchestra(
        trajectory_path=empty_path,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="r5",
        trace_dir=tmp_path,
        duration_ms=500,
        status="ok",  # status="ok" but trajectory missing → still flagged
    )

    start, messages, dispatches, media_events, end, audit_out = load_trace(trace_path)
    assert len(messages) == 1
    assert messages[0].message.role == "user"
    assert end is not None
    assert "error" in end.failure_modes


# ---------------------------------------------------------------------------
# 7. step_log status=500 marks the matching ToolResultBlock is_error=True
# ---------------------------------------------------------------------------


def test_step_log_status_500_marks_is_error(
    tmp_path,
    trajectory_path,
    step_log_path,
    task,
    audit_data,
):
    trace_path = translate_aorchestra(
        trajectory_path=trajectory_path,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="r6",
        trace_dir=tmp_path,
        duration_ms=1_000,
        status="ok",
    )

    start, messages, dispatches, media_events, end, audit_out = load_trace(trace_path)

    # Collect (tool_use_id, is_error) for each ToolResultBlock in the trace.
    pairs: dict[str, bool] = {}
    for m in messages:
        for b in m.message.content:
            if isinstance(b, ToolResultBlock):
                pairs[b.tool_use_id] = b.is_error

    # abc123 (status=200) → is_error False
    assert pairs.get("abc123") is False
    # def456 (status=500) → is_error True
    assert pairs.get("def456") is True


# ---------------------------------------------------------------------------
# 8. audit_data yields AuditSnapshot events keyed by service_name
# ---------------------------------------------------------------------------


def test_audit_data_yields_audit_snapshots(
    tmp_path,
    trajectory_path,
    step_log_path,
    task,
):
    audit_data = {
        "ocr": {
            "calls": [
                {
                    "endpoint": "/ocr/extract",
                    "request_body": {"image_path": "/data/x.pdf"},
                    "response_body": {"text": "stub"},
                    "status": 200,
                }
            ]
        }
    }
    trace_path = translate_aorchestra(
        trajectory_path=trajectory_path,
        step_log_path=step_log_path,
        audit_data=audit_data,
        task=task,
        run_id="r7",
        trace_dir=tmp_path,
        duration_ms=1_000,
        status="ok",
    )

    with trace_path.open() as fh:
        lines = [json.loads(line) for line in fh if line.strip()]

    snapshots = [ev for ev in lines if ev["type"] == "audit_snapshot"]
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap["service_name"] == "ocr"
    assert "audit_url" in snap and snap["audit_url"]
    assert snap["audit_data"] == audit_data["ocr"]
