"""Wave 3-D §6.5 end-to-end: drive T077 through OpenClawHarness for real.

This test is the acceptance gate for the entire Phase 3 OpenClaw path. It
exercises every piece (native runner subprocess, bridge plugin install via
``npm`` / ``openclaw plugins``, real LLM API, real mock OCR service, trace
translator, host snapshot) end-to-end with **no mocks**.

How to run
----------

```
RUN_E2E=1 python -m pytest tests/test_openclaw_e2e.py -p no:quadrants -v
```

The test is gated on the ``RUN_E2E`` env var because:

* it calls the real LLM endpoint configured via env vars (costs money / quota)
* it requires Node.js + ``openclaw`` CLI installed locally
* the bridge install runs ``npm install`` (network access, ~30s)

Acceptance criteria (any failure breaks the test):

1. **callID consistency**: each session.jsonl ``callID`` is also present in
   ``bridge_traffic.jsonl`` as ``toolCallId``. This validates the §3.5 Level 1
   matching assumption; Level 2 sequence fallback only matters if this fails.
2. **bridge log completeness**: ``len(session_toolcalls) == len(bridge_records)``
   (with no duplicate ids).
3. **task_score ≥ 0.3**: Anything lower means the trace translation or grader
   wiring is broken — T077's ground truth is 36080 and the keywords_present
   check alone is worth 0.55 of completion.
4. **snapshot ok**: ``collect_workdir_snapshot`` returns a dict, keys are
   shaped like ``cmd:``/``file:``/``local_file:``.

The test writes ``e2e_report.json`` into ``tmp_path`` so a human inspecting a
failed run can see all four signals + the underlying paths in one place.
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
# Fixtures: mock OCR service + LLM creds
# ---------------------------------------------------------------------------


OCR_PORT = 9121
# LLM credentials are read from env vars to keep secrets out of git history.
# Set CLAWEVAL_LLM_BASE_URL / CLAWEVAL_LLM_API_KEY / CLAWEVAL_LLM_MODEL before
# running with RUN_E2E=1. The test self-skips if any of the three are missing.
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

    The mock OCR fixture reads from ``OCR_FIXTURES`` + ``OCR_FILENAME`` and
    serves a fixed treasury bulletin text on POST /ocr/extract. We use the
    canonical T077 setup so the model sees ground-truth-relevant text.
    """
    fixtures_dir = REPO_ROOT / "tasks" / TASK_ID / "fixtures"
    env = {
        **os.environ,
        "OCR_FIXTURES": str(fixtures_dir),
        "OCR_FILENAME": "treasury_bulletin_1958_10.txt",
        "PORT": str(OCR_PORT),
    }
    # Strip proxy vars so the mock doesn't accidentally route through one.
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
        # Poll up to 15s for health.
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
# Helpers used by the test body
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _session_toolcalls(session_jsonl: Path) -> list[dict]:
    """Yield {callID, tool} pairs for every assistant toolCall in session.jsonl.

    The OpenClaw session format wraps toolCalls inside ``content`` blocks of
    assistant ``message`` events. We pull every ``toolCall`` block out and
    return a list of ``{"callID": str, "name": str}`` dicts.
    """
    calls: list[dict] = []
    for evt in _read_jsonl(session_jsonl):
        if evt.get("type") != "message":
            continue
        msg = evt.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "toolCall":
                cid = block.get("id")
                name = block.get("name")
                if isinstance(cid, str) and isinstance(name, str):
                    calls.append({"callID": cid, "name": name})
    return calls


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
def test_t077_openclaw_e2e(tmp_path, ocr_service):
    """End-to-end: prompt -> bridge plugin -> openclaw -> trace -> grade.

    Touches the real LLM API, the real OpenClaw CLI, real ``npm install``
    for the bridge plugin, and the real mock OCR service. No mocks.
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

    # Use the default repo config but rewrite the model section in memory.
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
    harness = get_harness("openclaw")
    preflight_errs = harness.preflight(task)
    assert preflight_errs == [], f"preflight should pass for T077: {preflight_errs}"

    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(parents=True)
    run_id = "e2e"

    # The ServiceManager would normally manage the mock OCR service, but we've
    # already started it via the ocr_service fixture; pass None for
    # services_ctx since the harness only reads audit endpoints from task
    # services anyway.
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

    # ---- Locate the auxiliary files we'll inspect ----
    case_dir = trace_dir / f"{TASK_ID}_{run_id}_raw"
    raw_dir = case_dir / "raw"
    bridge_log_path = raw_dir / "bridge_traffic.jsonl"
    session_jsonl = raw_dir / "session.jsonl"

    # ---- Check 1: callID consistency ----
    session_calls = _session_toolcalls(session_jsonl)
    bridge_records = _read_jsonl(bridge_log_path)
    bridge_ids = {rec.get("toolCallId") for rec in bridge_records if rec.get("toolCallId")}

    unmatched = [
        {"callID": c["callID"], "tool": c["name"]}
        for c in session_calls
        if c["callID"] not in bridge_ids
    ]
    callid_report = {
        "matched": len(session_calls) - len(unmatched),
        "unmatched": unmatched,
        "all_matched": len(unmatched) == 0 and len(session_calls) > 0,
        "session_calls": session_calls,
        "bridge_ids": sorted(bridge_ids),
    }

    # ---- Check 2: bridge log completeness ----
    bridge_log_complete = {
        "session_toolcalls": len(session_calls),
        "bridge_records": len(bridge_records),
        "ok": len(session_calls) == len(bridge_records) and len(session_calls) > 0,
    }

    # ---- Check 3: task_score sanity ----
    trace_path = result.trace_path
    start, messages, dispatches, media_events, end, audit_data = load_trace(trace_path)
    grader = get_grader(TASK_ID, tasks_dir=tasks_dir, task_dir=task_yaml.parent)
    # T077's grader calls judge.evaluate() unconditionally for the
    # reasoning_quality component. Use the same LLM endpoint as the agent so
    # we get a real judge score rather than tripping AttributeError on a
    # None judge.
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
    snapshot_ok = isinstance(snapshot, dict) and all(
        isinstance(k, str)
        and k.split(":", 1)[0] in {"cmd", "file", "local_file"}
        for k in snapshot.keys()
    )

    # Pull a final-text sample for the report (helps post-mortem when
    # task_score is low).
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
        "callID_consistency": callid_report,
        "bridge_log_complete": bridge_log_complete,
        "task_score": task_score,
        "scores": {
            "completion": scores.completion,
            "robustness": scores.robustness,
            "communication": scores.communication,
            "safety": scores.safety,
        },
        "snapshot_ok": snapshot_ok,
        "snapshot_keys": sorted(snapshot.keys()) if isinstance(snapshot, dict) else None,
        "session_jsonl_path": str(session_jsonl),
        "bridge_log_path": str(bridge_log_path),
        "trace_path": str(trace_path),
        "raw_dir": str(raw_dir),
        "final_assistant_text": final_text[:2000],
    }

    (tmp_path / "e2e_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ---- Now actually assert. Order matters: fail loudest on score so any
    # other failure shows up in the report too. ----
    failures: list[str] = []

    if not callid_report["all_matched"]:
        failures.append(
            f"callID consistency failed: "
            f"{callid_report['matched']} matched, "
            f"{len(callid_report['unmatched'])} unmatched"
        )
    if not bridge_log_complete["ok"]:
        failures.append(
            f"bridge log completeness failed: "
            f"{bridge_log_complete['session_toolcalls']} session toolCalls vs. "
            f"{bridge_log_complete['bridge_records']} bridge records"
        )
    if not snapshot_ok:
        failures.append(
            f"snapshot schema invalid: keys={list(snapshot.keys()) if isinstance(snapshot, dict) else snapshot!r}"
        )
    if task_score < 0.3:
        # Dump the full trace into case_dir for post-mortem.
        diag_path = case_dir / "e2e_full_trace_dump.json"
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
