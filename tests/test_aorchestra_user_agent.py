"""Tests for the AOrchestra arm's multi-turn simulated-user (user_agent) support.

Phase 4 Part 2 — each ``complete`` action is a user-turn boundary: the answer is
handed to ``UserAgent.generate_response``; a non-``None`` reply is injected into
``MainAgent.context`` (no reset) and the loop continues. ``[user_agent]``-prefixed
user messages must reach the final trace (hard dependency of the
``user_agent_clarify`` grader's clarify/answer phase split).

These tests do NOT call any real LLM — both the MainAgent and the UserAgent are
stubbed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from claw_eval.harnesses.aorchestra import _runner
from claw_eval.harnesses.aorchestra._trace_adapter import translate_aorchestra
from claw_eval.models.message import Message
from claw_eval.models.task import (
    Environment,
    Prompt,
    TaskDefinition,
    UserAgentTaskConfig,
)
from claw_eval.trace.reader import load_trace


# ---------------------------------------------------------------------------
# Fixtures / stubs
# ---------------------------------------------------------------------------


def _make_task(*, ua_enabled: bool, max_rounds: int = 3) -> TaskDefinition:
    return TaskDefinition(
        task_id="UA_test_task",
        task_name="user_agent multi-turn smoke",
        prompt=Prompt(text="帮我算一下我的退休金。"),
        environment=Environment(max_turns=10, timeout_seconds=300),
        user_agent=UserAgentTaskConfig(
            enabled=ua_enabled,
            persona="你是一个45岁想退休的用户。",
            max_rounds=max_rounds,
        ),
    )


class _StubUserAgent:
    """Returns scripted replies in order; ``None`` ends the conversation."""

    def __init__(self, replies: list[str | None]) -> None:
        self._replies = list(replies)
        self.calls: list[list[Message]] = []
        self.personas: list[str] = []

    def generate_response(
        self, persona: str, conversation_messages: list[Message]
    ) -> str | None:
        self.personas.append(persona)
        self.calls.append(list(conversation_messages))
        if not self._replies:
            return None
        return self._replies.pop(0)


class _StubMainAgent:
    """Minimal MainAgent stand-in: emits scripted ``complete`` actions.

    Records ``context`` mutations so the test can assert the user_agent reply
    was injected (and that ``reset`` was NOT called between turns).
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.context = ""
        self.attempt = 0
        self.image_contents: list = []
        self.context_history: list[str] = []
        self.reset_calls = 0

    def reset(self, info: Any) -> None:  # pragma: no cover - guard
        self.reset_calls += 1

    def get_usage_cost(self) -> float:
        return 0.0

    async def step(self, observation, history, **kwargs):
        self.attempt += 1
        # Snapshot the context the agent saw on entry to this step.
        self.context_history.append(self.context)
        ans = self._answers.pop(0) if self._answers else "final answer"
        action_result = {
            "action": "complete",
            "params": {"answer": ans},
            "result": {},
        }
        return action_result, f"raw:{ans}"


@pytest.fixture
def patched_main_agent(monkeypatch):
    """Patch run_one_task's MainAgent construction to return our stub.

    We bypass the heavy AOrchestra LLM/tool wiring: the stub's ``step`` is the
    only behaviour the loop exercises.
    """
    holder: dict[str, _StubMainAgent] = {}

    def _install(answers: list[str]) -> _StubMainAgent:
        stub = _StubMainAgent(answers)
        holder["agent"] = stub

        monkeypatch.setattr(_runner, "create_llm_instance", lambda *a, **k: object())
        monkeypatch.setattr(_runner, "LLMsConfig", _FakeLLMsConfig)
        monkeypatch.setattr(_runner, "MainAgent", lambda *a, **k: stub)
        monkeypatch.setattr(_runner, "DelegateTaskTool", lambda *a, **k: object())
        monkeypatch.setattr(_runner, "CompleteTool", lambda *a, **k: object())
        monkeypatch.setattr(_runner, "default_registry", lambda: {})
        # Skip image loading entirely.
        monkeypatch.setattr(_runner, "_load_task_images", lambda task, cfg: ([], []))
        return stub

    return _install


class _FakeLLMsConfig:
    @staticmethod
    def default():
        return _FakeLLMsConfig()

    def get(self, name):
        return object()


class _FakeEnv:
    """Stub ClawEvalEnv: only the attributes run_one_task touches."""

    instruction = ""
    meta_data: dict = {}

    def get_action_space_for(self, role):
        return []

    def tool_schemas(self):
        return {}


class _FakeModelCfg:
    model_id = "stub-model"
    input_modalities = ["text"]


class _FakeMediaCfg:
    enabled = False
    max_files = 0
    max_bytes_per_file = 0
    image_max_dimension = 0
    strict_mode = False


class _FakeCfg:
    model = _FakeModelCfg()
    media = _FakeMediaCfg()


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_does_not_reject_user_agent_task():
    from claw_eval.harnesses.aorchestra.harness import AOrchestraHarness

    task = _make_task(ua_enabled=True)
    assert AOrchestraHarness().preflight(task) == []


# ---------------------------------------------------------------------------
# _build_conversation_for_ua
# ---------------------------------------------------------------------------


def test_build_conversation_for_ua_orders_prompt_answer_reply():
    task = _make_task(ua_enabled=True)
    attempts = [
        {"action": "complete", "params": {"answer": "你的退休金约为X。"}, "result": {}},
        {"action": "user_agent_reply", "params": {}, "result": {"reply": "我还有社保。"}},
    ]
    conv = _runner._build_conversation_for_ua(attempts, task)
    roles = [m.role for m in conv]
    assert roles == ["user", "assistant", "user"]
    assert conv[0].text == task.prompt.text
    assert conv[1].text == "你的退休金约为X。"
    assert conv[2].text.startswith("[user_agent]")
    assert "我还有社保。" in conv[2].text


# ---------------------------------------------------------------------------
# _loop multi-turn behaviour (via run_one_task)
# ---------------------------------------------------------------------------


def _run(task, user_agent, patched_main_agent_install, answers):
    stub = patched_main_agent_install(answers)
    cfg = _FakeCfg()
    env = _FakeEnv()
    case_dir = Path("/tmp") / f"ua_test_{id(task)}"
    raw = asyncio.run(
        _runner.run_one_task(
            task,
            env,
            cfg,
            case_dir=case_dir,
            sandbox_url=None,
            user_agent=user_agent,
        )
    )
    return stub, raw


def test_loop_runs_user_agent_round_then_done(patched_main_agent):
    """First complete → user asks a follow-up; second complete → [DONE] ends."""
    task = _make_task(ua_enabled=True, max_rounds=3)
    ua = _StubUserAgent(["请问是哪一个退休计划?", None])

    stub, raw = _run(task, ua, patched_main_agent, ["初步答案", "完整答案"])

    # (b) loop triggered a user_agent round, injected context (no reset), then
    # ended cleanly on the second-turn [DONE].
    assert ua.calls and len(ua.calls) == 2
    # reset() is called exactly once, BEFORE the loop (run_one_task line ~668),
    # never between turns — the agent keeps its accumulated context.
    assert stub.reset_calls == 1
    assert "[user_agent]" in stub.context
    assert "请问是哪一个退休计划?" in stub.context
    # MainAgent stepped twice (one per complete).
    assert stub.attempt == 2
    assert raw["status"] == "ok"

    # (c) attempts_detail (persisted to trajectory) contains a user_agent_reply.
    import json as _json

    traj = _json.loads(Path(raw["trajectory_path"]).read_text())
    actions = [s["action"] for s in traj["trajectory"]]
    assert actions == ["complete", "user_agent_reply", "complete"]
    ua_step = traj["trajectory"][1]
    assert ua_step["result"]["reply"] == "请问是哪一个退休计划?"
    # Final answer is the last complete's answer.
    assert traj["final_answer"] == "完整答案"


def test_loop_without_user_agent_is_single_shot(patched_main_agent):
    """No user_agent → first complete ends the task (back-compat)."""
    task = _make_task(ua_enabled=False)
    stub, raw = _run(task, None, patched_main_agent, ["唯一答案", "不该出现"])

    assert stub.attempt == 1
    import json as _json

    traj = _json.loads(Path(raw["trajectory_path"]).read_text())
    actions = [s["action"] for s in traj["trajectory"]]
    assert actions == ["complete"]
    assert traj["final_answer"] == "唯一答案"


def test_loop_respects_max_rounds(patched_main_agent):
    """max_rounds=1 → exactly one user_agent round, then terminal on next complete."""
    task = _make_task(ua_enabled=True, max_rounds=1)
    # UserAgent would keep asking, but the loop must stop after 1 round.
    ua = _StubUserAgent(["追问1", "追问2", "追问3"])

    stub, raw = _run(task, ua, patched_main_agent, ["a1", "a2", "a3"])

    assert len(ua.calls) == 1  # only the first complete consults the user
    import json as _json

    traj = _json.loads(Path(raw["trajectory_path"]).read_text())
    actions = [s["action"] for s in traj["trajectory"]]
    assert actions == ["complete", "user_agent_reply", "complete"]
    assert traj["final_answer"] == "a2"


# ---------------------------------------------------------------------------
# trace adapter emits the [user_agent] marker
# ---------------------------------------------------------------------------


def test_trace_adapter_emits_user_agent_marker(tmp_path):
    """A user_agent_reply trajectory step becomes a [user_agent]-prefixed user msg."""
    task = _make_task(ua_enabled=True)
    traj = {
        "task_id": task.task_id,
        "main_model": "stub-model",
        "success": True,
        "final_answer": "完整答案",
        "media": [],
        "trajectory": [
            {
                "attempt": 1,
                "action": "complete",
                "params": {"answer": "初步答案"},
                "result": {},
                "raw_response": "raw1",
            },
            {
                "attempt": 1,
                "action": "user_agent_reply",
                "params": {},
                "result": {"reply": "请问是哪一个?"},
                "raw_response": "",
            },
            {
                "attempt": 2,
                "action": "complete",
                "params": {"answer": "完整答案"},
                "result": {},
                "raw_response": "raw2",
            },
        ],
    }
    import json as _json

    traj_path = tmp_path / "traj.json"
    traj_path.write_text(_json.dumps(traj, ensure_ascii=False))

    trace_path = translate_aorchestra(
        trajectory_path=traj_path,
        step_log_path=None,
        audit_data={},
        task=task,
        run_id="r1",
        trace_dir=tmp_path,
        duration_ms=1000,
        status="ok",
    )

    _start, messages, _disp, _media, _end, _audit = load_trace(trace_path)

    marker_msgs = [
        m
        for m in messages
        if m.message.role == "user"
        and (m.message.text or "").startswith("[user_agent]")
    ]
    assert len(marker_msgs) == 1
    assert "请问是哪一个?" in marker_msgs[0].message.text

    # Ordering: the marker comes after the first complete's assistant message
    # and before the second complete's assistant message.
    texts = [m.message.text for m in messages]
    idx_first = next(i for i, t in enumerate(texts) if "初步答案" in (t or ""))
    idx_marker = next(
        i for i, t in enumerate(texts) if (t or "").startswith("[user_agent]")
    )
    idx_second = next(i for i, t in enumerate(texts) if "完整答案" in (t or ""))
    assert idx_first < idx_marker < idx_second
