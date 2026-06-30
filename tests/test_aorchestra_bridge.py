"""Wave 4-B unit tests for the AOrchestra bridge module.

Phase 4 — see docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md
and docs/superpowers/plans/2026-06-24-aorchestra-harness.md.
"""
from __future__ import annotations

import asyncio
import sys

import httpx
import pytest
import respx
from httpx import Response

# AOrchestra is not a pip package — inject its source root on sys.path before
# any aorchestra-* import. See docs/superpowers/specs/aorchestra_decision.md.
_AORCHESTRA_ROOT = "/data2/ruanjianhao/AOrchestra"
if _AORCHESTRA_ROOT not in sys.path:
    sys.path.insert(0, _AORCHESTRA_ROOT)

from claw_eval.models.content import TextBlock
from claw_eval.models.message import Message
from claw_eval.models.tool import ToolEndpoint, ToolSpec
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


# ---------------------------------------------------------------------------
# Task 3: HTTP + sandbox action factories
# ---------------------------------------------------------------------------


@pytest.fixture
def ocr_tool_spec():
    return ToolSpec(
        name="ocr_extract_text",
        description="OCR a file",
        input_schema={"type": "object", "properties": {"image_path": {"type": "string"}}},
    )


@pytest.fixture
def ocr_endpoint():
    return ToolEndpoint(
        tool_name="ocr_extract_text",
        url="http://mock-ocr/ocr/extract",
        method="POST",
    )


def test_make_http_action_returns_base_action_with_metadata(ocr_tool_spec, ocr_endpoint):
    from claw_eval.harnesses.aorchestra._bridge.actions import make_http_action

    step_log: list[dict] = []
    action = make_http_action(ocr_tool_spec, ocr_endpoint, step_log)
    assert action.name == "ocr_extract_text"
    assert action.description == "OCR a file"
    assert action.parameters == ocr_tool_spec.input_schema


@respx.mock
def test_http_action_call_records_step_log_on_success(ocr_tool_spec, ocr_endpoint):
    from claw_eval.harnesses.aorchestra._bridge.actions import make_http_action

    respx.post("http://mock-ocr/ocr/extract").mock(
        return_value=Response(200, json={"text": "hello"})
    )
    step_log: list[dict] = []
    action = make_http_action(ocr_tool_spec, ocr_endpoint, step_log, agent_role="main")
    result = asyncio.run(action(image_path="x.pdf"))
    assert result == {"text": "hello"}
    assert len(step_log) == 1
    rec = step_log[0]
    assert rec["tool"] == "ocr_extract_text"
    assert rec["url"] == "http://mock-ocr/ocr/extract"
    assert rec["method"] == "POST"
    assert rec["status"] == 200
    assert rec["response"] == {"text": "hello"}
    assert rec["request"] == {"image_path": "x.pdf"}
    assert isinstance(rec["toolCallId"], str) and len(rec["toolCallId"]) == 32
    assert rec["agent_role"] == "main"
    assert "durationMs" in rec
    assert rec.get("error") is None


@respx.mock
def test_http_action_transport_error_records_status_minus_one(ocr_tool_spec, ocr_endpoint):
    from claw_eval.harnesses.aorchestra._bridge.actions import make_http_action

    respx.post("http://mock-ocr/ocr/extract").mock(side_effect=httpx.ConnectError("boom"))
    step_log: list[dict] = []
    action = make_http_action(ocr_tool_spec, ocr_endpoint, step_log)
    result = asyncio.run(action(image_path="x.pdf"))
    assert isinstance(result, dict) and "error" in result
    assert len(step_log) == 1
    rec = step_log[0]
    assert rec["status"] == -1
    assert rec["error"] is not None


def test_make_sandbox_action_routes_to_sandbox_exec():
    from claw_eval.harnesses.aorchestra._bridge.actions import (
        SANDBOX_ENDPOINTS,
        make_sandbox_action,
    )

    tool = ToolSpec(
        name="Bash",
        description="run bash",
        input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
    )
    step_log: list[dict] = []
    action = make_sandbox_action(tool, "http://sandbox:8080", step_log)
    assert action.name == "Bash"
    # The URL is baked in at construction; check via SANDBOX_ENDPOINTS map.
    assert SANDBOX_ENDPOINTS["Bash"] == "/exec"


def test_make_sandbox_action_raises_when_sandbox_url_missing():
    from claw_eval.harnesses.aorchestra._bridge.actions import (
        SchemaTranslationError,
        make_sandbox_action,
    )

    tool = ToolSpec(
        name="Bash",
        description="run bash",
        input_schema={"type": "object", "properties": {}},
    )
    step_log: list[dict] = []
    with pytest.raises(SchemaTranslationError):
        make_sandbox_action(tool, None, step_log)


def test_sandbox_endpoints_cover_all_sandbox_tool_names():
    from claw_eval.harnesses.aorchestra._bridge.actions import SANDBOX_ENDPOINTS
    from claw_eval.runner.sandbox_tools import SANDBOX_TOOL_NAMES

    for name in SANDBOX_TOOL_NAMES:
        assert name in SANDBOX_ENDPOINTS, (
            f"SANDBOX_ENDPOINTS missing {name!r} — check SandboxToolDispatcher._PATH_MAP"
        )


@respx.mock
def test_http_action_non_json_response_passes_through_as_text(ocr_tool_spec, ocr_endpoint):
    from claw_eval.harnesses.aorchestra._bridge.actions import make_http_action

    respx.post("http://mock-ocr/ocr/extract").mock(
        return_value=Response(200, text="plain text body")
    )
    step_log: list[dict] = []
    action = make_http_action(ocr_tool_spec, ocr_endpoint, step_log)
    result = asyncio.run(action(image_path="x.pdf"))
    assert result == "plain text body"
    assert step_log[0]["status"] == 200
    assert step_log[0]["response"] == "plain text body"



# ---------------------------------------------------------------------------
# Task 4: ClawEvalEnv adapter
# ---------------------------------------------------------------------------


from pathlib import Path  # noqa: E402

from claw_eval.models.task import TaskDefinition  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def t077_task():
    return TaskDefinition.from_yaml(
        _REPO_ROOT / "tasks" / "T077_officeqa_highest_dept_spending" / "task.yaml"
    )


@pytest.fixture
def t068_task():
    return TaskDefinition.from_yaml(
        _REPO_ROOT / "tasks" / "T068zh_llama_w8a8_cuda_bug" / "task.yaml"
    )


def test_clawevalenv_reset_returns_task_prompt(t077_task):
    from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv

    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        obs = env.reset()
        assert obs == t077_task.prompt.text


def test_clawevalenv_get_action_space_matches_task_tools(t077_task):
    from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv

    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        actions = env.get_action_space()
        names = {a.name for a in actions}
        assert names == {t.name for t in t077_task.tools}


def test_clawevalenv_get_action_space_for_main_and_sub_are_distinct(t077_task):
    from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv

    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        main_actions = env.get_action_space_for("main")
        sub_actions = env.get_action_space_for("sub")
        assert main_actions is not sub_actions
        assert {a.name for a in main_actions} == {a.name for a in sub_actions}


def test_clawevalenv_step_log_starts_empty(t077_task):
    from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv

    with ClawEvalEnv(t077_task, sandbox_url=None) as env:
        assert env.step_log() == []


def test_clawevalenv_rejects_sandbox_tools_without_sandbox_url(t068_task):
    from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv
    from claw_eval.harnesses.aorchestra._bridge.actions import SchemaTranslationError

    # T068 declares Bash → must raise when sandbox_url is None
    with ClawEvalEnv(t068_task, sandbox_url=None) as env:
        with pytest.raises(SchemaTranslationError):
            env.get_action_space()


def test_clawevalenv_accepts_sandbox_tools_when_sandbox_url_present(t068_task):
    from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv
    from claw_eval.runner.sandbox_tools import SANDBOX_TOOL_NAMES

    with ClawEvalEnv(t068_task, sandbox_url="http://sandbox:8080") as env:
        actions = env.get_action_space()
        names = {a.name for a in actions}
        # Container mode (sandbox_url set): the action space is the task's
        # declared tools PLUS the full SANDBOX_TOOLS set (deduped), giving the
        # agent toolset parity with the baseline. Using a set comparison also
        # proves there are no duplicate actions for tools the task declares.
        assert names == {t.name for t in t068_task.tools} | set(SANDBOX_TOOL_NAMES)
        # No action is emitted twice (dedup against task-declared names).
        assert len([a.name for a in actions]) == len(names)


def test_clawevalenv_use_after_close_raises(t077_task):
    from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv

    env = ClawEvalEnv(t077_task, sandbox_url=None)
    env.__enter__()
    env.__exit__(None, None, None)
    with pytest.raises(RuntimeError):
        env.reset()


# ---------------------------------------------------------------------------
# Task 5: LLMsConfig patch (decision.md revision: single entry only)
# ---------------------------------------------------------------------------


from base.engine.async_llm import LLMsConfig  # noqa: E402

from claw_eval.config import ModelConfig  # noqa: E402


@pytest.fixture
def claweval_cfg_model():
    return ModelConfig(
        model_id="claude-sonnet-4-5",
        api_key="sk-test-12345",
        base_url="https://newapi.example.com/v1",
    )


def test_build_llms_config_has_primary_entry(claweval_cfg_model):
    from claw_eval.harnesses.aorchestra._bridge.model_config import build_llms_config

    cfg = build_llms_config(claweval_cfg_model)
    primary = cfg.get("claude-sonnet-4-5")
    assert primary.base_url == "https://newapi.example.com/v1"
    assert primary.key == "sk-test-12345"
    assert primary.model == "claude-sonnet-4-5"


def test_build_llms_config_does_not_expose_gemini_alias(claweval_cfg_model):
    """Spec decision 9 (gemini alias) was reversed during Wave 4-A — AOrchestra
    delegate.py was patched to use self.models[0] instead, so we no longer
    need a gemini entry."""
    from claw_eval.harnesses.aorchestra._bridge.model_config import build_llms_config

    cfg = build_llms_config(claweval_cfg_model)
    # Asking for the legacy key should fail outright.
    with pytest.raises(ValueError):
        cfg.get("gemini-3-flash-preview")


def test_build_llms_config_alias_key_tracks_model_id():
    """Phase 5: the alias dict key is now cfg.model.model_id (not a hardcoded
    'claude-sonnet-4-5'). Pointing claw-eval at a local vllm server with a
    different served name (e.g. 'qwen3.6-27b') must produce a working
    LLMsConfig entry under that exact name."""
    from claw_eval.harnesses.aorchestra._bridge.model_config import build_llms_config

    qwen_cfg = ModelConfig(
        model_id="qwen3.6-27b",
        api_key="EMPTY",
        base_url="http://localhost:8001/v1",
    )
    cfg = build_llms_config(qwen_cfg)
    primary = cfg.get("qwen3.6-27b")
    assert primary.base_url == "http://localhost:8001/v1"
    assert primary.key == "EMPTY"
    assert primary.model == "qwen3.6-27b"
    # The old hardcoded entry must NOT exist when a different model_id is used.
    with pytest.raises(ValueError):
        cfg.get("claude-sonnet-4-5")


def test_build_llms_config_api_key_defaults_to_empty_placeholder():
    """vllm and some private endpoints don't require an api key; we use the
    'EMPTY' placeholder (accepted by openai-sdk) when the config omits one."""
    from claw_eval.harnesses.aorchestra._bridge.model_config import build_llms_config

    no_key_cfg = ModelConfig(
        model_id="local-model",
        api_key="",
        base_url="http://localhost:8001/v1",
    )
    cfg = build_llms_config(no_key_cfg)
    primary = cfg.get("local-model")
    assert primary.key == "EMPTY"


def test_patched_llms_config_restores_default_on_exit(claweval_cfg_model):
    from claw_eval.harnesses.aorchestra._bridge.model_config import patched_llms_config

    previous = LLMsConfig._default_config
    with patched_llms_config(claweval_cfg_model) as cfg:
        assert LLMsConfig._default_config is cfg
        assert cfg.get("claude-sonnet-4-5").base_url == "https://newapi.example.com/v1"
    assert LLMsConfig._default_config is previous


def test_patched_llms_config_restores_on_exception(claweval_cfg_model):
    from claw_eval.harnesses.aorchestra._bridge.model_config import patched_llms_config

    previous = LLMsConfig._default_config
    with pytest.raises(RuntimeError):
        with patched_llms_config(claweval_cfg_model):
            raise RuntimeError("simulated crash")
    assert LLMsConfig._default_config is previous
