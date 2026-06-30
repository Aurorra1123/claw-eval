"""Phase 4 Wave 4-D §5.3 e2e: drive T077 through AOrchestraHarness host mode.

The acceptance gate for the AOrchestra host-smoke integration. Mirrors
``tests/test_openclaw_e2e.py`` in shape but adapts the four checks for the
AOrchestra-specific data model:

1. ``trace_path`` exists and ``load_trace()`` returns a valid 6-tuple.
2. At least one TraceMessage or ToolDispatch has ``agent_role`` in
   ``{"main", "sub"}`` — verifies §3 trace-translation table fills the field.
3. ``task_score >= 0.3``. T077 ground truth is "36080"; the keywords grader
   alone is worth 0.55 if the model surfaces it, so anything below 0.3 means
   the trace pipeline is broken.
4. ``env_snapshot`` is either ``None`` (T077 doesn't declare snapshot fields)
   or has the canonical ``cmd:`` / ``file:`` / ``local_file:`` key shape.

Plus one soft signal recorded into ``e2e_report.json`` but NOT asserted:

5. ``delegate_count`` — number of assistant messages whose content includes a
   ToolUseBlock with ``name="delegate_task"``. Per §5.3 / Q6 decision, LLM
   delegation isn't a contract-level requirement, so we record it for
   post-mortem visibility only.

How to run
----------

::

    export CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1
    export CLAWEVAL_LLM_API_KEY=...
    export CLAWEVAL_LLM_MODEL=claude-sonnet-4-5
    export AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra
    RUN_E2E=1 python -m pytest tests/test_aorchestra_e2e.py -p no:quadrants -v

The test self-skips without ``RUN_E2E`` + the three ``CLAWEVAL_LLM_*`` vars.
No docker gate — AOrchestra runs in-process on the host.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Fixtures: OCR mock service (verbatim from test_openclaw_e2e.py) + LLM creds
# ---------------------------------------------------------------------------


OCR_PORT = 9121
LLM_BASE_URL = os.environ.get("CLAWEVAL_LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("CLAWEVAL_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("CLAWEVAL_LLM_MODEL", "")
TASK_ID = "T077_officeqa_highest_dept_spending"


def _port_in_use(port: int) -> bool:
    """Return True if a TCP socket can connect to (127.0.0.1, port)."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


@pytest.fixture
def ocr_service():
    """Start the OCR mock service if not already running.

    Verbatim from ``tests/test_openclaw_e2e.py::ocr_service`` so the two e2e
    tests use byte-identical fixture surface.
    """
    fixtures_dir = REPO_ROOT / "tasks" / TASK_ID / "fixtures"
    env = {
        **os.environ,
        "OCR_FIXTURES": str(fixtures_dir),
        "OCR_FILENAME": "treasury_bulletin_1958_10.txt",
        "PORT": str(OCR_PORT),
    }
    for key in (
        "http_proxy", "https_proxy", "all_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    ):
        env.pop(key, None)

    proc = None
    if not _port_in_use(OCR_PORT):
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "mock_services" / "ocr" / "server.py")],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _port_in_use(OCR_PORT):
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise RuntimeError(f"OCR service exited: {stderr[:500]}")
            time.sleep(0.3)
        else:
            proc.terminate()
            raise RuntimeError("OCR service failed to come up within 15s")

    yield f"http://127.0.0.1:{OCR_PORT}"

    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_delegate_tool_uses(messages) -> int:
    """Return the number of assistant messages that include a delegate_task
    ToolUseBlock. Used for the soft delegate-count signal.
    """
    count = 0
    for msg in messages:
        if msg.message.role != "assistant":
            continue
        for block in msg.message.content or []:
            name = getattr(block, "name", None)
            block_type = getattr(block, "type", None)
            if block_type == "tool_use" and name == "delegate_task":
                count += 1
                break
    return count


# ---------------------------------------------------------------------------
# The actual e2e test
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="set RUN_E2E=1 to run end-to-end test",
)
@pytest.mark.skipif(
    not (
        os.environ.get("CLAWEVAL_LLM_BASE_URL")
        and os.environ.get("CLAWEVAL_LLM_API_KEY")
        and os.environ.get("CLAWEVAL_LLM_MODEL")
    ),
    reason="set CLAWEVAL_LLM_BASE_URL / CLAWEVAL_LLM_API_KEY / CLAWEVAL_LLM_MODEL",
)
def test_t077_aorchestra_host_e2e(tmp_path, ocr_service):
    """End-to-end: prompt -> AOrchestra MainAgent -> trace -> grade.

    Touches the real LLM API, the real AOrchestra Python library
    (no subprocess), and the real mock OCR service. No mocks.
    """
    from claw_eval.config import load_config
    from claw_eval.graders.llm_judge import LLMJudge
    from claw_eval.graders.registry import get_grader
    from claw_eval.harnesses import get_harness
    from claw_eval.models.scoring import compute_task_score
    from claw_eval.models.task import TaskDefinition
    from claw_eval.trace.reader import load_trace

    # ---- Load task + override config to point at the real LLM ----
    task_yaml = REPO_ROOT / "tasks" / TASK_ID / "task.yaml"
    task = TaskDefinition.from_yaml(task_yaml)
    tasks_dir = REPO_ROOT / "tasks"

    cfg = load_config(REPO_ROOT / "config_general.yaml")
    cfg = cfg.model_copy(
        update={
            "model": cfg.model.model_copy(
                update={
                    "api_key": LLM_API_KEY,
                    "base_url": LLM_BASE_URL,
                    "model_id": LLM_MODEL,
                },
            )
        }
    )

    # ---- Drive the harness ----
    harness = get_harness("aorchestra")
    preflight_errs = harness.preflight(task)
    assert preflight_errs == [], f"preflight should pass for T077: {preflight_errs}"

    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(parents=True)
    run_id = "e2e"

    result = harness.run(
        task,
        trace_dir=trace_dir,
        run_id=run_id,
        cfg=cfg,
        sandbox_handle=None,
        user_agent=None,
        services_ctx=None,
        sandbox_tools=False,
    )

    # ---- Check 1: trace exists and loads ----
    trace_path = result.trace_path
    assert trace_path.exists(), f"trace file missing: {trace_path}"
    start, messages, dispatches, media_events, end, audit_data = load_trace(trace_path)
    trace_loads = (
        start is not None
        and end is not None
        and isinstance(messages, list)
        and isinstance(dispatches, list)
    )

    # ---- Check 2: agent_role filled on at least one event ----
    agent_roles_seen = set()
    for msg in messages:
        ar = getattr(msg, "agent_role", "agent")
        if ar in ("main", "sub"):
            agent_roles_seen.add(ar)
    for disp in dispatches:
        ar = getattr(disp, "agent_role", "agent")
        if ar in ("main", "sub"):
            agent_roles_seen.add(ar)
    agent_role_ok = bool(agent_roles_seen)

    # ---- Check 3: task_score sanity ----
    grader = get_grader(TASK_ID, tasks_dir=tasks_dir, task_dir=task_yaml.parent)
    judge = LLMJudge(
        model_id=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
    )
    scores = grader.grade(
        messages=messages,
        dispatches=dispatches,
        task=task,
        audit_data=audit_data,
        judge=judge,
        media_events=media_events,
        env_snapshot=result.env_snapshot,
    )
    task_score = compute_task_score(scores)

    # ---- Check 4: snapshot schema ----
    snapshot = result.env_snapshot
    if snapshot is None:
        snapshot_ok = True  # T077 has no env_snapshot_* declarations
    elif isinstance(snapshot, dict):
        snapshot_ok = all(
            isinstance(k, str)
            and k.split(":", 1)[0] in {"cmd", "file", "local_file"}
            for k in snapshot.keys()
        )
    else:
        snapshot_ok = False

    # ---- Soft signal: delegate count ----
    delegate_count = _count_delegate_tool_uses(messages)

    # ---- Final-text sample for post-mortem ----
    final_text = ""
    for msg in reversed(messages):
        if msg.message.role == "assistant":
            for block in msg.message.content:
                txt = getattr(block, "text", None)
                if isinstance(txt, str) and txt.strip():
                    final_text = txt
                    break
            if final_text:
                break

    report = {
        "trace_loads": trace_loads,
        "agent_roles_seen": sorted(agent_roles_seen),
        "agent_role_ok": agent_role_ok,
        "delegate_count": delegate_count,
        "task_score": task_score,
        "scores": {
            "completion": scores.completion,
            "robustness": scores.robustness,
            "communication": scores.communication,
            "safety": scores.safety,
        },
        "snapshot_ok": snapshot_ok,
        "snapshot_keys": sorted(snapshot.keys()) if isinstance(snapshot, dict) else None,
        "trace_path": str(trace_path),
        "raw_dir": str(result.raw_dir) if result.raw_dir else None,
        "messages_count": len(messages),
        "dispatches_count": len(dispatches),
        "final_assistant_text": final_text[:2000],
    }

    (tmp_path / "e2e_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ---- Now assert. delegate_count is soft (recorded only) per §5.3 / Q6. ----
    failures: list[str] = []

    if not trace_loads:
        failures.append("trace_path exists but load_trace() did not return valid tuple")
    if not agent_role_ok:
        failures.append(
            "no TraceMessage or ToolDispatch has agent_role in {'main','sub'} — "
            "trace adapter is not filling the field"
        )
    if not snapshot_ok:
        failures.append(
            f"snapshot schema invalid: keys="
            f"{list(snapshot.keys()) if isinstance(snapshot, dict) else snapshot!r}"
        )
    if task_score < 0.3:
        diag_path = tmp_path / "e2e_full_trace_dump.json"
        diag = {
            "trace_path": str(trace_path),
            "messages_count": len(messages),
            "dispatches_count": len(dispatches),
            "final_text": final_text,
            "scores": report["scores"],
        }
        diag_path.write_text(json.dumps(diag, indent=2, ensure_ascii=False))
        failures.append(
            f"task_score below threshold: {task_score:.3f} < 0.3 "
            f"(diagnostic dump: {diag_path})"
        )

    if failures:
        report_path = tmp_path / "e2e_report.json"
        pytest.fail(
            "E2E acceptance failures:\n  - "
            + "\n  - ".join(failures)
            + f"\n\nFull report: {report_path}"
        )
