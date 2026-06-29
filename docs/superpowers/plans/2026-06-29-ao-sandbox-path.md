# AO Sandbox Path (Wave 4-E) Implementation Plan

> **For agentic workers:** execute task-by-task; each ends with a runnable verification.

**Goal:** Implement AOrchestra harness's container/sandbox path so AO can give its agent the full `SANDBOX_TOOLS` set (Bash/Read/Write/Edit/Glob/Grep/...), matching the baseline's toolset for a fair comparison.

**Architecture:** Almost all building blocks exist (ClawEvalEnv sandbox dispatch, SandboxRunner, fixture injection, CLI container orchestration, runtime tool-agnosticism). Two gaps: (A) the `_run_container` body (currently `NotImplementedError`), (B) appending the full SANDBOX_TOOLS set when sandbox is enabled (AO currently only exposes `task.tools`).

**Tech Stack:** claw-eval at `/data2/ruanjianhao/claw-eval/`, AO harness at `src/claw_eval/harnesses/aorchestra/`. Reference: `docs/ao_sandbox_path_estimate.md` (commit `d15257f`).

## Global Constraints

- Implementation site is **claw-eval only** (`harnesses/aorchestra/`). Do NOT touch AOrchestra repo, other harnesses, or task.yaml files.
- The sandbox server (`src/claw_eval/sandbox/server.py`) accepts BOTH `file_path` and `path` for read (`req.file_path or req.path`), and `timeout_seconds` for exec. AO POSTs LLM kwargs verbatim via `make_sandbox_action`/`SandboxToolDispatcher._PATH_MAP`. Claude-style `file_path` works as-is; no param shim needed for read/write. (Only `timeout` ms and grep aliases would need translation — out of scope; office_qa tasks use Read/Bash/Grep with standard names.)
- `_run_host_smoke` is the working reference for the agent loop. `_run_container` mirrors it with `sandbox_url` threaded in.
- Container env_snapshot is owned by the CLI (the CLI collects it from the sandbox server). `_run_container` returns `env_snapshot=None`.
- pytest needs `-p no:quadrants`, never `-v`.
- claw-eval git identity is configured (no env-var dance).
- Sonnet creds for verification: `CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1`, `CLAWEVAL_LLM_API_KEY=sk-u3HGm150NcbCJu2ohLB6BIgktG8V3QmVz8LA5JSTeDQXHhEo`, `CLAWEVAL_LLM_MODEL=claude-sonnet-4-5`, `CLAWEVAL_AORCHESTRA_RUNTIME=pi`, `AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra`, `no_proxy=localhost,127.0.0.1`.

---

## Task 1: Gap B — append SANDBOX_TOOLS when sandbox is enabled

**Files:** `src/claw_eval/harnesses/aorchestra/_bridge/env.py` (the `ClawEvalEnv._build_actions` / construction path), possibly `_runner.py` where the env is built.

**Interfaces:**
- Consumes: `SANDBOX_TOOLS` from `claw_eval.runner.sandbox_tools`, `SANDBOX_TOOL_NAMES`.
- Produces: when `ClawEvalEnv` is built with a `sandbox_url` (container mode), `get_action_space_for(role)` returns the task's declared tools PLUS the full SANDBOX_TOOLS set (deduped against task-declared names), each as a `make_sandbox_action(...)`.

- [ ] **Step 1: Read the current `_build_actions` + how `sandbox_url` is set**

```bash
cd /data2/ruanjianhao/claw-eval
sed -n '/def _build_actions/,/return actions/p' src/claw_eval/harnesses/aorchestra/_bridge/env.py
sed -n '/def __init__/,/_endpoint_by_name/p' src/claw_eval/harnesses/aorchestra/_bridge/env.py
```
Note: `make_sandbox_action(tool, self._sandbox_url, self._step_log, agent_role=...)` already exists and is used for task-declared sandbox tools. The mock-service `claweval` loop appends the full set in `runner/loop.py:261-268` — mirror that intent.

- [ ] **Step 2: Implement the append**

In `_build_actions(agent_role)`, after building actions from `self._task.tools`, if `self._sandbox_url` is set, append the full `SANDBOX_TOOLS` set (deduped against the task's already-declared tool names) as sandbox actions. Sketch (adapt to actual code):

```python
from claw_eval.runner.sandbox_tools import SANDBOX_TOOLS  # at top of file

# ... inside _build_actions, after the existing task.tools loop ...
if self._sandbox_url:
    declared = {t.name for t in self._task.tools}
    for spec in SANDBOX_TOOLS:
        if spec.name in declared:
            continue  # task already declares it; don't double-add
        actions.append(make_sandbox_action(
            spec, self._sandbox_url, self._step_log, agent_role=agent_role,
        ))
```

Only append when `sandbox_url` is set, so host-mode behaviour is unchanged (host mode has no sandbox_url and still refuses sandbox tools at the gate).

- [ ] **Step 3: Verify import + no host-mode regression**

```bash
cd /data2/ruanjianhao/claw-eval
python -c "from claw_eval.harnesses.aorchestra._bridge.env import ClawEvalEnv; print('import ok')"
python -m pytest tests/ -p no:quadrants --tb=short 2>&1 | tail -5
```
Expected: import ok; 105 passed / 4 skipped (no regression — host-mode env still builds only task.tools since sandbox_url is None in those tests).

- [ ] **Step 4: Commit**

```bash
git add src/claw_eval/harnesses/aorchestra/_bridge/env.py
git commit -m "feat(aorchestra): append SANDBOX_TOOLS in container mode (toolset parity)

When ClawEvalEnv has a sandbox_url (container mode), expose the full
SANDBOX_TOOLS set (Bash/Read/Write/Edit/Glob/Grep/...) to the agent,
deduped against task-declared tools — mirroring claweval's loop.py:261-268.
This matches the baseline's toolset so AO office_qa scores are comparable.
Host mode (sandbox_url=None) is unchanged."
```

## Task 2: Gap A — implement `_run_container`

**Files:** `src/claw_eval/harnesses/aorchestra/harness.py` (`_run_container`, currently raises `NotImplementedError` ~line 231-245).

**Interfaces:**
- Consumes: `sandbox_handle: ContainerHandle` (has `.sandbox_url`), `services_ctx`, the same `_runner.run_one_task` the host path uses, `ClawEvalEnv` (now sandbox-aware from Task 1).
- Produces: a populated `HarnessResult` (trace written, scored) for a task run inside the container with sandbox tools.

- [ ] **Step 1: Read `_run_host_smoke` (the reference) + `_run_container` stub + ContainerHandle**

```bash
cd /data2/ruanjianhao/claw-eval
sed -n '/def _run_host_smoke/,/def _run_container/p' src/claw_eval/harnesses/aorchestra/harness.py
grep -n "sandbox_url\|class ContainerHandle\|\.sandbox_url" src/claw_eval/runner/sandbox_runner.py | head
```
Understand: host_smoke builds `ClawEvalEnv(task, sandbox_url=None)`, calls `run_one_task(...)`, writes the trace, returns a HarnessResult. Container path is the same but `sandbox_url=sandbox_handle.sandbox_url`, the host SystemExit gate is absent, and env_snapshot is None.

- [ ] **Step 2: Implement `_run_container`**

Replace the `raise NotImplementedError(...)` body with a near-copy of `_run_host_smoke`, with these differences:
- Build the env with the sandbox url: `ClawEvalEnv(task, sandbox_url=sandbox_handle.sandbox_url)` (use the actual attribute name from ContainerHandle — confirm it's `.sandbox_url` in Step 1).
- Do NOT include the host-mode sandbox-tool refusal gate.
- Pass `services_ctx` through to wherever host_smoke uses it.
- Return the HarnessResult with `env_snapshot=None` (CLI owns container snapshot).
Reuse every helper host_smoke uses (`_runner.run_one_task`, trace adapter, `_write_step_log`, etc.). Do not reimplement them.

Write the actual code by reading host_smoke and adapting — keep the structure identical so the only diffs are the three points above.

- [ ] **Step 3: Verify it imports + the NotImplementedError is gone**

```bash
cd /data2/ruanjianhao/claw-eval
python -c "from claw_eval.harnesses.aorchestra.harness import AOrchestraHarness; print('ok')"
python -m pytest tests/ -p no:quadrants --tb=short 2>&1 | tail -5
```
Expected: ok; 105 passed / 4 skipped.

- [ ] **Step 4: Commit**

```bash
git add src/claw_eval/harnesses/aorchestra/harness.py
git commit -m "feat(aorchestra): implement _run_container (Wave 4-E sandbox path)

Mirror _run_host_smoke but build ClawEvalEnv with sandbox_handle.sandbox_url
so sandbox tools (from Task 1's append) dispatch to the in-container sandbox
server. Drop the host-mode sandbox refusal; return env_snapshot=None (CLI
owns container snapshot). Reuses run_one_task, trace adapter, step-log writer
unchanged."
```

## Task 3: End-to-end verification on a sandbox office_qa task

**Files:** none (verification).

- [ ] **Step 1: Run one office_qa task through AO container path with sandbox**

T082 is the canonical case (baseline used Bash/Read/grep/sed → 0.92; our OCR-only AO got 0.80). Run it with `--sandbox` so AO now has the full toolset:

```bash
cd /data2/ruanjianhao/claw-eval
source .venv/bin/activate
AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra \
CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1 \
CLAWEVAL_LLM_API_KEY=sk-u3HGm150NcbCJu2ohLB6BIgktG8V3QmVz8LA5JSTeDQXHhEo \
CLAWEVAL_LLM_MODEL=claude-sonnet-4-5 \
CLAWEVAL_AORCHESTRA_RUNTIME=pi \
no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1 \
python -m claw_eval.cli batch \
  --tasks-dir tasks --task-ids T082_officeqa_qoq_esf_change \
  --harness aorchestra --sandbox \
  --config config_concurrency_smoke.yaml \
  --parallel 1 --trials 1 \
  --trace-dir /data2/ruanjianhao/claw-eval/traces/ao_sandbox_verify 2>&1 | tail -30
```
Generous timeout (1800000 ms). Needs the sandbox image — confirm `docker images claw-eval-agent:latest` (or the openclaw image, whichever the CLI defaults AO to) exists first; if missing, report.

- [ ] **Step 2: Confirm sandbox tools were actually used**

Inspect the trace / step_log: did the AO agent call `Bash`/`Read`/`Grep` (not just `ocr_extract_text`)? Did the container dispatch them (sandbox server hits)? Capture the task_score.

```bash
RAW=$(find traces/ao_sandbox_verify -name "step_log.jsonl" | head -1)
python3 -c "
import json
from collections import Counter
c=Counter()
for line in open('$RAW'):
    try: c[json.loads(line).get('tool','?')]+=1
    except: pass
print('AO sandbox T082 tools used:', dict(c))
"
```
Expected: sandbox tools (Bash/Read/Grep) appear → toolset parity achieved. Score should move toward baseline's 0.92 (exact parity not required; the point is AO now CAN use the tools). Report the score + tool usage.

- [ ] **Step 3: Clean up + write a short result note**

`docker rm -f` any leftover claw-agent-* (leave paper_eval). Append findings to `docs/ao_sandbox_path_estimate.md` (or a new `docs/ao_sandbox_verify.md`): the T082 score with sandbox vs the prior OCR-only 0.80, the tools the agent used, and whether AO is now ready for a toolset-matched office_qa rerun. Commit.

---

## Self-Review

- Gap B (Task 1): SANDBOX_TOOLS appended only when sandbox_url set → host-mode unchanged. ✓
- Gap A (Task 2): `_run_container` mirrors host_smoke + sandbox_url + no gate + env_snapshot None. ✓
- R5 (param translation): sandbox server accepts `file_path`/`path` natively → no shim needed for Read/Write; Bash uses `command`/`timeout_seconds`. Verified via server.py. ✓
- Verification (Task 3): T082 with --sandbox proves sandbox tools reach the agent and dispatch. ✓
- No task.yaml / other-harness / AOrchestra-repo changes. ✓
