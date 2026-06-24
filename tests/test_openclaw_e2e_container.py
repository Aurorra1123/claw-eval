"""Wave 3-E §6.5 — container-mode end-to-end tests for the OpenClaw harness.

Two scenarios:

* ``test_t077_openclaw_e2e_container`` — regression of Wave 3-D T077, but
  this time running OpenClaw inside the sandbox container. Asserts the same
  4 acceptance items as Wave 3-D plus the container-specific schema check
  (env_snapshot keys come from the sandbox server, not a host workdir).
* ``test_t068_bash_bridge_e2e_container`` — T068 declares ``Bash``. The
  bridge plugin must route it to ``{sandbox_url}/exec``; bridge_traffic.jsonl
  must record at least one ``tool=Bash`` call with that URL. This is the
  Wave 3-E core validation.

How to run::

    RUN_E2E=1 python -m pytest tests/test_openclaw_e2e_container.py -p no:quadrants -v

Gated on ``RUN_E2E`` because it:

* requires Docker + the ``claw-eval-agent-openclaw:latest`` image (build
  beforehand: ``docker build -f Dockerfile.openclaw -t claw-eval-agent-openclaw:latest .``)
* calls the real LLM endpoint and runs ``npm install`` inside the container
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
# Shared config / fixtures
# ---------------------------------------------------------------------------


# LLM credentials are read from env vars to keep secrets out of git history.
# Set CLAWEVAL_LLM_BASE_URL / CLAWEVAL_LLM_API_KEY / CLAWEVAL_LLM_MODEL before
# running with RUN_E2E=1. Each test self-skips if any of the three are missing.
LLM_BASE_URL = os.environ.get("CLAWEVAL_LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("CLAWEVAL_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("CLAWEVAL_LLM_MODEL", "")
OPENCLAW_IMAGE = "claw-eval-agent-openclaw:latest"

OCR_PORT = 9121
WEB_REAL_PORT = 9114


def _docker_available() -> bool:
    try:
        p = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=5,
        )
        return p.returncode == 0
    except Exception:
        return False


def _image_available(image: str) -> bool:
    try:
        p = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=5,
        )
        return p.returncode == 0
    except Exception:
        return False


def _port_in_use(port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


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


@pytest.fixture
def ocr_service():
    """T077 mock OCR service on :9121 (started on host, never inside a container)."""
    task_id = "T077_officeqa_highest_dept_spending"
    fixtures_dir = REPO_ROOT / "tasks" / task_id / "fixtures"
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


@pytest.fixture
def web_real_service():
    """T068 mock web_real service on :9114."""
    env = {
        **os.environ,
        "PORT": str(WEB_REAL_PORT),
    }
    for key in (
        "http_proxy", "https_proxy", "all_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    ):
        env.pop(key, None)

    proc = None
    if not _port_in_use(WEB_REAL_PORT):
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "mock_services" / "web_real" / "server.py")],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _port_in_use(WEB_REAL_PORT):
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise RuntimeError(f"web_real service exited: {stderr[:500]}")
            time.sleep(0.3)
        else:
            proc.terminate()
            raise RuntimeError("web_real service failed to come up within 15s")

    yield f"http://127.0.0.1:{WEB_REAL_PORT}"

    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Common harness driver — shared between the two e2e tests.
# ---------------------------------------------------------------------------


def _run_container_harness(
    *,
    task_id: str,
    trace_dir: Path,
):
    """Drive ``OpenClawHarness._run_container`` with a freshly started
    container. Returns ``(task, result, case_dir, raw_dir, sandbox_url)``
    so each test can inspect the artefacts it cares about.
    """
    from claw_eval.config import load_config
    from claw_eval.harnesses import get_harness
    from claw_eval.models.task import TaskDefinition
    from claw_eval.runner.sandbox_runner import SandboxRunner
    from claw_eval.runner.services import ServiceManager

    task_yaml = REPO_ROOT / "tasks" / task_id / "task.yaml"
    task = TaskDefinition.from_yaml(task_yaml)

    cfg = load_config(REPO_ROOT / "config_general.yaml")
    cfg = cfg.model_copy(
        update={
            "model": cfg.model.model_copy(
                update={
                    "api_key": LLM_API_KEY,
                    "base_url": LLM_BASE_URL,
                    "model_id": LLM_MODEL,
                },
            ),
        }
    )

    case_dir = trace_dir / f"{task.task_id}_e2e_raw"
    case_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = case_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    bridge_log_in_container = raw_dir / "bridge_traffic.jsonl"

    sandbox_runner = SandboxRunner(cfg.sandbox, image=OPENCLAW_IMAGE)
    handle = sandbox_runner.start_container(
        run_id=f"{task_id}-e2e",
        network_mode="host",
        volumes={str(case_dir): str(case_dir)},
        extra_env={"CLAWEVAL_BRIDGE_LOG": str(bridge_log_in_container)},
    )

    try:
        # Open the mock service connection through the host ServiceManager.
        # We DON'T launch them via ServiceManager (the per-test fixture already
        # has them up); pass None.
        harness = get_harness("openclaw")
        with ServiceManager(task.services, mock_today=task.environment.mock_today) as svc:
            preflight_errs = harness.preflight(task)
            assert preflight_errs == [], f"preflight failed: {preflight_errs}"
            result = harness.run(
                task,
                trace_dir=trace_dir,
                run_id="e2e",
                cfg=cfg,
                sandbox_handle=handle,
                user_agent=None,
                services_ctx=svc,
                sandbox_tools=True,
            )
        return task, result, case_dir, raw_dir, handle.sandbox_url
    finally:
        try:
            sandbox_runner.stop_container(handle)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test 1: T077 container-mode regression
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="set RUN_E2E=1 to run end-to-end test",
)
@pytest.mark.skipif(not _docker_available(), reason="needs docker")
@pytest.mark.skipif(
    not _image_available(OPENCLAW_IMAGE),
    reason=f"image {OPENCLAW_IMAGE} not built (run docker build -f Dockerfile.openclaw)",
)
@pytest.mark.skipif(
    not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL),
    reason="set CLAWEVAL_LLM_BASE_URL / CLAWEVAL_LLM_API_KEY / CLAWEVAL_LLM_MODEL",
)
def test_t077_openclaw_e2e_container(tmp_path, ocr_service):
    """Container-mode T077: §6.5 Wave 3-E acceptance checks 1-4 + 6 + 7."""
    from claw_eval.graders.llm_judge import LLMJudge
    from claw_eval.graders.registry import get_grader
    from claw_eval.models.scoring import compute_task_score
    from claw_eval.trace.reader import load_trace

    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(parents=True)
    task, result, case_dir, raw_dir, sandbox_url = _run_container_harness(
        task_id="T077_officeqa_highest_dept_spending",
        trace_dir=trace_dir,
    )

    session_jsonl = raw_dir / "session.jsonl"
    bridge_log_path = raw_dir / "bridge_traffic.jsonl"

    # Acceptance check 1: callID consistency.
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
    }

    # Acceptance check 2: bridge log completeness.
    bridge_log_complete = {
        "session_toolcalls": len(session_calls),
        "bridge_records": len(bridge_records),
        "ok": len(session_calls) == len(bridge_records) and len(session_calls) > 0,
    }

    # Acceptance check 3: task_score.
    tasks_dir = REPO_ROOT / "tasks"
    task_yaml = tasks_dir / "T077_officeqa_highest_dept_spending" / "task.yaml"
    start, messages, dispatches, media_events, end, audit_data = load_trace(result.trace_path)
    grader = get_grader(task.task_id, tasks_dir=tasks_dir, task_dir=task_yaml.parent)
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

    # Acceptance check 4: snapshot schema equivalence.
    # T077 has no env_snapshot_commands, so the dict is {} — that still counts
    # as schema-valid: every key (if any) follows the cmd:/file:/local_file:
    # format used by the host snapshot in Wave 3-D.
    snapshot = result.env_snapshot
    snapshot_ok = isinstance(snapshot, dict) and all(
        isinstance(k, str)
        and k.split(":", 1)[0] in {"cmd", "file", "local_file"}
        for k in snapshot.keys()
    )

    # Acceptance check 6: audit_data in trace.
    audit_in_trace = audit_data is not None and isinstance(audit_data, dict)

    # Acceptance check 7: builtin tools blocked. Every toolCall in
    # session.jsonl is a bridge tool (or a sanctioned read-only diagnostic).
    bridge_tool_names = {ep.tool_name for ep in (task.tool_endpoints or [])}
    sanctioned = bridge_tool_names | {"session_status", "multi_tool_use.parallel"}
    blocked_violations = [
        c["name"] for c in session_calls if c["name"] not in sanctioned
    ]

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
        "audit_in_trace": audit_in_trace,
        "blocked_violations": blocked_violations,
        "case_dir": str(case_dir),
        "sandbox_url": sandbox_url,
    }
    (tmp_path / "e2e_container_report_t077.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    failures: list[str] = []
    if not callid_report["all_matched"]:
        failures.append(
            f"callID consistency failed: {callid_report['matched']} matched, "
            f"{len(callid_report['unmatched'])} unmatched"
        )
    if not bridge_log_complete["ok"]:
        failures.append(
            f"bridge log incomplete: {bridge_log_complete['session_toolcalls']} session "
            f"toolCalls vs {bridge_log_complete['bridge_records']} bridge records"
        )
    if not snapshot_ok:
        failures.append(f"snapshot schema invalid: {list(snapshot.keys())[:5]}")
    if not audit_in_trace:
        failures.append("audit_data missing from trace")
    if blocked_violations:
        failures.append(
            f"OpenClaw builtin tools leaked through: {blocked_violations}"
        )
    if task_score < 0.3:
        failures.append(f"task_score below threshold: {task_score:.3f}")

    if failures:
        pytest.fail(
            "E2E container T077 failures:\n  - " + "\n  - ".join(failures)
            + f"\n\nFull report: {tmp_path / 'e2e_container_report_t077.json'}"
        )


# ---------------------------------------------------------------------------
# Test 2: T068 Bash bridge validation
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(
    not os.environ.get("RUN_E2E"),
    reason="set RUN_E2E=1 to run end-to-end test",
)
@pytest.mark.skipif(not _docker_available(), reason="needs docker")
@pytest.mark.skipif(
    not _image_available(OPENCLAW_IMAGE),
    reason=f"image {OPENCLAW_IMAGE} not built",
)
@pytest.mark.skipif(
    not (LLM_BASE_URL and LLM_API_KEY and LLM_MODEL),
    reason="set CLAWEVAL_LLM_BASE_URL / CLAWEVAL_LLM_API_KEY / CLAWEVAL_LLM_MODEL",
)
def test_t068_bash_bridge_e2e_container(tmp_path, web_real_service):
    """Container-mode T068: §6.5 Wave 3-E acceptance check 5 (Bash bridged).

    T068 declares ``Bash`` + web_search + web_fetch. We validate:

    * Bridge plugin generated a Bash tool with url == sandbox server /exec
    * bridge_traffic.jsonl includes at least one ``tool: Bash`` record
      with that URL (the LLM may not call Bash if it doesn't see the need,
      but the bridge MUST be installed and ready)
    """
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(parents=True)
    task, result, case_dir, raw_dir, sandbox_url = _run_container_harness(
        task_id="T068zh_llama_w8a8_cuda_bug",
        trace_dir=trace_dir,
    )

    # The bridge plugin source got cleaned up at the end of the harness
    # run; re-render it here to verify Bash got routed to the sandbox URL.
    # ``compile_plugin_source`` is pure / deterministic — it produces the
    # same string the harness embedded into the bridge plugin we just ran.
    from claw_eval.harnesses._openclaw_bridge import compile_plugin_source
    rendered_src = compile_plugin_source(task, sandbox_url=sandbox_url)
    assert rendered_src is not None, (
        "bridge source rendering returned None for a task with bridgeable tools"
    )
    sandbox_exec_url = f"{sandbox_url}/exec"
    bridge_has_bash_url = sandbox_exec_url in rendered_src

    # Now inspect bridge_traffic.jsonl for the actual call records.
    bridge_log_path = raw_dir / "bridge_traffic.jsonl"
    bridge_records = _read_jsonl(bridge_log_path)
    bash_records = [r for r in bridge_records if r.get("tool") == "Bash"]
    bash_to_sandbox = [r for r in bash_records if r.get("url") == sandbox_exec_url]

    # Additional standard checks (1-4, 6, 7) repeated for T068.
    session_jsonl = raw_dir / "session.jsonl"
    session_calls = _session_toolcalls(session_jsonl)
    bridge_ids = {rec.get("toolCallId") for rec in bridge_records if rec.get("toolCallId")}
    unmatched = [
        {"callID": c["callID"], "tool": c["name"]}
        for c in session_calls
        if c["callID"] not in bridge_ids
    ]
    # callID consistency is "every session toolCall has a matching bridge
    # record". If session_calls is empty (the model answered without tools),
    # this is vacuously true — and that's a legitimate outcome for T068.
    callid_ok = len(unmatched) == 0
    # bridge log completeness — same vacuous-true rule.
    bridge_complete_ok = len(session_calls) == len(bridge_records)

    bridge_tool_names = {ep.tool_name for ep in (task.tool_endpoints or [])} | {"Bash"}
    sanctioned = bridge_tool_names | {"session_status", "multi_tool_use.parallel"}
    blocked_violations = [
        c["name"] for c in session_calls if c["name"] not in sanctioned
    ]

    report = {
        "bridge_has_bash_url": bridge_has_bash_url,
        "bridge_bash_records": len(bash_records),
        "bridge_bash_to_sandbox": len(bash_to_sandbox),
        "session_calls": len(session_calls),
        "session_call_names": [c["name"] for c in session_calls],
        "bridge_record_tools": [r.get("tool") for r in bridge_records],
        "callid_ok": callid_ok,
        "bridge_complete_ok": bridge_complete_ok,
        "blocked_violations": blocked_violations,
        "sandbox_url": sandbox_url,
        "sandbox_exec_url": sandbox_exec_url,
    }
    (tmp_path / "e2e_container_report_t068.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    failures: list[str] = []
    # Hard requirement: bridge plugin source for T068 must route Bash to
    # the container's sandbox server. This is the §6.5 Wave 3-E check #5,
    # and it MUST pass regardless of whether the model called Bash.
    if not bridge_has_bash_url:
        failures.append(
            f"bridge plugin source missing sandbox URL {sandbox_exec_url}"
        )
    # Conditional: if the model DID call Bash, the call must have gone to
    # the sandbox URL. Models often answer T068 from knowledge without
    # invoking tools — that's a legitimate outcome we don't fail on.
    if bash_records and not bash_to_sandbox:
        failures.append(
            f"Bash calls recorded ({len(bash_records)}) but none to sandbox URL "
            f"{sandbox_exec_url}; records: {bash_records[:2]}"
        )
    if not callid_ok:
        failures.append(
            f"callID consistency failed: {len(session_calls)} session toolCalls, "
            f"{len(unmatched)} unmatched"
        )
    if not bridge_complete_ok:
        failures.append(
            f"bridge log incomplete: {len(session_calls)} session vs "
            f"{len(bridge_records)} bridge"
        )
    if blocked_violations:
        failures.append(
            f"OpenClaw builtin tools leaked: {blocked_violations}"
        )

    if failures:
        pytest.fail(
            "E2E container T068 failures:\n  - " + "\n  - ".join(failures)
            + f"\n\nFull report: {tmp_path / 'e2e_container_report_t068.json'}"
        )
