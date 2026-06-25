"""Wave 4-B unit tests for the AOrchestra bridge module.

Phase 4 — see docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md
and docs/superpowers/plans/2026-06-24-aorchestra-harness.md.
"""
from __future__ import annotations

from claw_eval.models.content import TextBlock
from claw_eval.models.message import Message
from claw_eval.models.trace import ToolDispatch, TraceMessage


# ---------------------------------------------------------------------------
# Task 2: agent_role field on TraceMessage and ToolDispatch
# ---------------------------------------------------------------------------


def test_trace_message_agent_role_default_is_agent():
    msg = TraceMessage(
        trace_id="t1",
        message=Message(role="assistant", content=[TextBlock(text="hi")]),
    )
    assert msg.agent_role == "agent"


def test_trace_message_agent_role_accepts_main_and_sub():
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


def test_tool_dispatch_agent_role_accepts_main_and_sub():
    td = ToolDispatch(
        trace_id="t1",
        tool_use_id="tu1",
        tool_name="ocr_extract_text",
        endpoint_url="http://localhost:9121/ocr/extract",
        agent_role="sub",
    )
    assert td.agent_role == "sub"
