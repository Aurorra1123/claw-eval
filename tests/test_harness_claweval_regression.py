"""Wave 1 regression: ClawEvalHarness must be a byte-equivalent wrapper.

Run each of a small task set twice — once through ``run_task(...)`` directly,
once through ``get_harness("claweval").run(...)`` — and assert the two trace
JSONL files differ ONLY in noise (timestamps, trace_id, file path). All
event payloads must match.

The LLM is mocked: ``provider.chat`` returns a fixed text-only assistant
message so the loop terminates after one turn and no tools / services /
sandbox containers are touched. That keeps the test self-contained and
deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claw_eval.config import MediaConfig, ModelConfig, PromptConfig
from claw_eval.harnesses import get_harness
from claw_eval.harnesses.claweval import ClawEvalHarness
from claw_eval.models.content import TextBlock
from claw_eval.models.message import Message
from claw_eval.models.task import TaskDefinition
from claw_eval.models.trace import TokenUsage
from claw_eval.runner.loop import run_task


# The five tasks driven by both code paths. They span Chinese / English and
# different categories but share the trait that mocking the LLM to emit no
# tool_use keeps run_task in its simplest one-turn shape — no services, no
# sandbox, no user_agent invocation.
TASKS = [
    "C01zh_mortgage_prepay",
    "C02zh_personal_finance",
    "C03en_real_estate_finance",
    "C05zh_personal_finance_2",
    "C10zh_labor_law",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "tasks"


class _StubProvider:
    """OpenAICompatProvider stand-in. Always returns the same one-turn reply."""

    model_id = "stub-model"

    def chat(self, messages, tools=None):
        reply = Message(
            role="assistant",
            content=[TextBlock(text="OK, no further action required.")],
        )
        return reply, TokenUsage(input_tokens=10, output_tokens=5)


@pytest.fixture
def stub_provider():
    return _StubProvider()


@pytest.fixture
def stub_cfg():
    """A minimal Config-like shim that ClawEvalHarness consumes.

    We only need the model/prompt/media slices the harness threads through to
    run_task — the heavier judge / sandbox / user_agent_model fields are
    unused in this path.
    """

    class _CfgStub:
        model = ModelConfig(model_id="stub-model")
        prompt = PromptConfig()
        media = MediaConfig()

    return _CfgStub()


def _normalize(raw_lines: list[str]) -> list[dict]:
    """Strip volatile fields from each trace event for cross-path comparison.

    The fields we strip vary by event-type but in every case carry pure noise
    (timestamps, monotonic clock readings, generated UUIDs). Everything else
    — message content, dispatches, token usage, turn count, harness name —
    must match identically between the two paths.
    """

    NOISE_KEYS = {
        "timestamp",
        "trace_id",
        # TraceEnd totals derived from wall-clock / monotonic measurements:
        "wall_time_s",
        "model_time_s",
        "tool_time_s",
        "other_time_s",
    }
    normalized = []
    for line in raw_lines:
        if not line.strip():
            continue
        ev = json.loads(line)
        for k in list(ev.keys()):
            if k in NOISE_KEYS:
                ev[k] = None
        normalized.append(ev)
    return normalized


@pytest.mark.parametrize("task_id", TASKS)
def test_claweval_harness_matches_direct_run_task(
    tmp_path, monkeypatch, stub_provider, stub_cfg, task_id
):
    task_yaml = TASKS_DIR / task_id / "task.yaml"
    if not task_yaml.exists():
        pytest.skip(f"task {task_id} not present in tasks/")

    task = TaskDefinition.from_yaml(task_yaml)

    # ---- Path A: legacy direct run_task call ----
    trace_dir_a = tmp_path / "direct"
    trace_dir_a.mkdir()
    trace_a = run_task(
        task,
        stub_provider,
        trace_dir=trace_dir_a,
        sandbox_tools=False,
        prompt_cfg=stub_cfg.prompt,
        model_cfg=stub_cfg.model,
        media_cfg=stub_cfg.media,
        user_agent=None,
    )

    # ---- Path B: through ClawEvalHarness ----
    # Force the harness to use our stub provider instead of building a real one.
    monkeypatch.setattr(
        ClawEvalHarness, "_build_provider", lambda self, cfg: stub_provider
    )
    harness = get_harness("claweval")
    trace_dir_b = tmp_path / "harness"
    trace_dir_b.mkdir()
    result = harness.run(
        task,
        trace_dir=trace_dir_b,
        run_id=f"{task.task_id}-regression",
        cfg=stub_cfg,
        sandbox_handle=None,
        user_agent=None,
        services_ctx=None,
        sandbox_tools=False,
    )
    trace_b = result.trace_path

    # ---- Compare ----
    lines_a = Path(trace_a).read_text().splitlines()
    lines_b = Path(trace_b).read_text().splitlines()

    assert len(lines_a) == len(lines_b), (
        f"event count differs: direct={len(lines_a)} harness={len(lines_b)}"
    )

    norm_a = _normalize(lines_a)
    norm_b = _normalize(lines_b)

    for i, (a, b) in enumerate(zip(norm_a, norm_b)):
        assert a == b, (
            f"event {i} (type={a.get('type')}) differs between direct and "
            f"harness paths after timestamp/id normalization:\n"
            f"  direct:  {a}\n  harness: {b}"
        )

    # And specifically: the new TraceStart.harness field must be "claweval"
    # on BOTH sides. The direct path goes through run_task which doesn't set
    # it explicitly — it falls back to the field default.
    start_a = json.loads(lines_a[0])
    start_b = json.loads(lines_b[0])
    assert start_a.get("harness") == "claweval"
    assert start_b.get("harness") == "claweval"
    # Result also exposes a non-None trace_path
    assert result.trace_path == trace_b
    assert result.audit_data == {}
    assert result.env_snapshot is None
    assert result.raw_dir is None
