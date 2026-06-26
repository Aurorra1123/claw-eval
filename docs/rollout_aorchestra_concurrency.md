# AOrchestra Harness — Concurrency (Parallel Batch) Investigation

**Date:** 2026-06-26
**Author:** investigation subagent (prep for sonnet rollout scale-up)
**Scope:** Verify the `aorchestra` harness in claw-eval runs correctly under concurrent
(`cmd_batch` / `ProcessPoolExecutor`) execution, for both `react` and `pi` SubAgent
runtimes. This is a **concurrency smoke**, not the real rollout.

**Verdict (TL;DR): SAFE to run a concurrent sonnet rollout.** No concurrency corruption
observed; the only fix needed is operational (must pass `--port-base-offset` to coexist with
other batch jobs, and must NOT pass `--no-judge` for tasks with an `llm_judge` scoring
component — see Hazard 6 / pre-existing grader bug). Recommended rollout command at the bottom.

---

## 1. Concurrency architecture (how batch parallelism works for the AO harness)

`python -m claw_eval.cli batch` → `cmd_batch` (`src/claw_eval/cli.py:1263`).

- **Process pool, not threads.** `ProcessPoolExecutor(max_workers=workers)`
  (`cli.py:1411`). On Linux the default multiprocessing start method is **fork**
  — confirmed there is **no `set_start_method` / `get_context` / `multiprocessing`**
  call anywhere in `src/claw_eval/` (grep clean). Fork means:
  - Each worker is a **separate OS process** with its own copy of all module-level
    globals and its own `os.environ` (inherited from the parent at fork time).
  - Per-process globals that the AO bridge mutates — `LLMsConfig._default_config`
    (the swap in `_bridge/model_config.py:patched_llms_config`) and the runtime
    selector read from `os.environ` — are therefore **not shared** between concurrent
    tasks. Each task process sees its own copy. This is the single most important
    safety fact, and it is verified, not assumed (see Hazards 4 & 5).
- **Slot → port offset.** Each worker slot gets a unique offset
  `offset = port_base_offset + slot * _STRIDE` with `_STRIDE = 50` (`cli.py:1422,1435`).
  The pool recycles `available_slots`, so at most `--parallel` distinct offsets are
  live at once. `task.apply_port_offset(offset)` (`cli.py:922`, in the worker
  `_run_single_task`) shifts the task's service ports + endpoint URLs so concurrent
  tasks never bind the same port.
- **`--port-base-offset`** lets multiple independent batch jobs coexist (this
  investigation used **500**; the parallel OpenClaw investigation uses 0).
- **Worker entry:** `_run_single_task` (`cli.py:870`) re-imports everything fresh
  inside the worker, applies the port offset, builds the judge, runs
  `harness.run(...)`, grades, and returns a result dict. The AOrchestra harness host
  path is `AOrchestraHarness._run_host_smoke` (`harnesses/aorchestra/harness.py:131`).

---

## 2. Hazards checked (verdict + evidence)

### Hazard 1 — Port offsetting shifts BOTH the service and its endpoint URLs. **SAFE.**

`TaskDefinition.apply_port_offset` (`src/claw_eval/models/task.py:138-164`):

- For every `service`: `svc.port += offset`, `svc.health_check`/`svc.reset_endpoint`
  rewritten via `_shift_url` (regex `localhost:(\d+)` → `localhost:port+offset`), and
  `svc.env["PORT"] = str(svc.port)` so the subprocess binds the shifted port.
- For every `tool_endpoint`: `ep.url = _shift_url(ep.url)`.

So for T077, which declares `service ocr_t51 port 9121` (`health_check`/`reset` at
`localhost:9121`) **and** `tool_endpoints.url = http://localhost:9121/ocr/extract`,
the **same offset** is applied to all four, keeping them consistent. A mismatch (agent
calling a port with no service) is structurally impossible because one regex + one
`+= offset` drives all of them. **Confirmed empirically:** both smoke batches made
successful OCR calls with zero "connection refused" errors at offsets 500/550.

### Hazard 2 — Pi worker (Node subprocess) concurrency. **SAFE.**

(Investigated via subagent reading `/data2/ruanjianhao/AOrchestra/aorchestra/runtime/`.)

- `pi_runtime.py`: `run_id = uuid.uuid4().hex` per `delegate_task` — random, no
  collision. Talks to the Node worker over **stdio pipes** (`asyncio.create_subprocess_exec`
  with `stdin/stdout/stderr=PIPE`), **not ports, not a Unix socket, not a temp file**.
  No hardcoded `/tmp/` path, no fixed-name JSON/log file, no `bind()`/`listen()`.
- `pi_worker/dist/index.js`: receives input on stdin, emits on stdout. Module-level
  `pendingResolvers` Map and `callIdCounter` are **per-process** — each `delegate_task`
  spawns a *new* Node process, so these never collide across delegations. (They would
  only collide if two delegations shared one Node runtime, which the architecture never
  does.)
- Under batch: each task is its own Python process; within a task the MainAgent runs
  delegations sequentially. So at most one Pi Node worker per task process at a time,
  and different tasks' Node workers are fully isolated by OS process boundaries +
  private stdio pipes. **Confirmed empirically:** pi batch ran T077 + T078 concurrently
  with no collision; T077 finished at 60s while T078 ran to 300s (true parallelism).

### Hazard 3 — Trace/raw output paths are unique per concurrent task. **SAFE.**

`run_id = f"{task.task_id}-t{i}-p{port_offset}"` (`cli.py:1028`, host-mode batch path).
`AOrchestraHarness._run_host_smoke` writes its raw dir as
`trace_dir / f"{task.task_id}_{run_id}_raw"` (`harness.py:163`) and the trace JSONL is
named from the same run_id. Uniqueness comes from two independent factors:
(a) different tasks have different `task.task_id`; (b) the same task can't run on two
slots at once, and even if it did the `-p<offset>` suffix differs.
**Confirmed empirically** — the two concurrent tasks produced:
```
T077_..._highest_dept_spending-t0-p500.jsonl   + ..._-t0-p500_raw/
T078_..._max_yield_spread-t0-p550.jsonl        + ..._-t0-p550_raw/
```
Distinct files, distinct dirs. No shared-file write.

### Hazard 4 — `CLAWEVAL_AORCHESTRA_RUNTIME` env-var timing. **SAFE.**

Read at `src/claw_eval/harnesses/aorchestra/_runner.py:399`:
`runtime_name = os.environ.get("CLAWEVAL_AORCHESTRA_RUNTIME", "react")`.
Because `ProcessPoolExecutor` uses **fork** on Linux (no `set_start_method` anywhere —
grep clean), worker processes **inherit the parent's `os.environ`**. So exporting the
var before launching the CLI propagates to every worker. **Confirmed empirically:** the
react batch logged `[DelegateTool] runtime=react` and the pi batch logged
`[DelegateTool] runtime=pi`, matching the exported value each time.
*Caveat for the future:* if claw-eval ever switches to `spawn`, the var would still be
inherited (Python re-exports parent env on spawn too), but the bridge `sys.path` and
`LLMsConfig` re-import would happen fresh — current `fork` behaviour is what was tested.

### Hazard 5 — Shared mutable state across tasks. **SAFE.**

- Harness instances are module-level singletons (`_REGISTRY` in
  `harnesses/__init__.py:33`), one `AOrchestraHarness()` per process. But the harness is
  **stateless w.r.t. tasks**: `_run_host_smoke` takes all task state as arguments and
  keeps nothing on `self` (only `name`/`supported_features` class attrs are read).
  Reviewed `harness.py` end-to-end — no `self.<task-state> = ...` assignment anywhere.
- `LLMsConfig._default_config` swap (`_bridge/model_config.py:patched_llms_config`) is a
  **class attribute** mutation, but under fork each worker has its own imported copy of
  the `LLMsConfig` class, so the swap is process-local. The contextmanager also restores
  the previous value on exit. **SAFE** under batch.
- (Same-process-only hazards flagged by the subagent — Node `callIdCounter`, ReAct
  `env.instruction` swap, the `LLMsConfig.default()` first-call TOCTOU — are **not
  reachable** under `ProcessPoolExecutor`, because each task is a separate process and
  runs its delegations sequentially. They would only matter if claw-eval used a
  *thread* pool, which it does not.)

### Hazard 6 (found during smoke) — `--no-judge` + `llm_judge` scoring component. **PRE-EXISTING GRADER BUG, not concurrency. Operational workaround applied.**

First react batch run (`--no-judge`) errored on BOTH tasks with
`'NoneType' object has no attribute 'evaluate'`. Root cause is **not** concurrency:
`tasks/T077_officeqa_highest_dept_spending/grader.py:86` calls `judge.evaluate(...)`
unconditionally inside `_call_judge`, invoked from `grade()` at line 127. With
`--no-judge` the judge is `None` (`cli.py:928`), so `None.evaluate(...)` raises. T077
and T078 both have an `llm_judge` scoring component (weight 0.35), so both die at the
grading stage regardless of parallelism — a single `--no-judge` run would fail the same
way. **Workaround (no code change):** enable the judge and point it at the same sonnet
endpoint. Verdict: harness is fine; the bug is in the per-task grader's missing
`if judge is None` guard. Flagged as a follow-up below.

---

## 3. Smoke run results

Environment for all runs:
```
AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra
CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1
CLAWEVAL_LLM_API_KEY=sk-...          (sonnet via deepwisdom, verified working today)
CLAWEVAL_LLM_MODEL=claude-sonnet-4-5
no_proxy=localhost,127.0.0.1  NO_PROXY=localhost,127.0.0.1
```
claw-eval was installed into a fresh `.venv` (`uv pip install -e ".[aorchestra,mock]"`).
A throwaway config `config_concurrency_smoke.yaml` wires model **and judge** to the
sonnet endpoint via `${CLAWEVAL_LLM_*}`. Tasks chosen: **T077** (OCR, answer 36080,
base port 9121) and **T078** (OCR, answer 031969, base port 9122) — both host-mode (only
tool is `ocr_extract_text`, no SANDBOX_TOOL_NAMES), distinct expected answers + distinct
OCR documents, so any cross-task leak would show up as a wrong answer.

### 3a. Single-task react sanity (port-offset 500)
```
python -m claw_eval.cli run --task tasks/T077_officeqa_highest_dept_spending \
  --harness aorchestra --config config_concurrency_smoke.yaml --no-judge \
  --port-offset 500 --trace-dir traces/smoke_single_react
```
Result: stack ran end-to-end. MainAgent → ReAct SubAgent → OCR service (377k chars
extracted) → service stopped cleanly. `runtime=react` honoured, offset 500 applied,
trace written. task_score 0.0 (react looped on analysis and never emitted 36080 in a
FINAL_ANSWER — a react quality issue, not infra). Purpose (prove the stack works)
achieved.

### 3b. react 2-task concurrent batch — FIRST attempt (the debug trail)
```
CLAWEVAL_AORCHESTRA_RUNTIME=react python -m claw_eval.cli batch --range 77-78 \
  --harness aorchestra --config config_concurrency_smoke.yaml --no-judge \
  --parallel 2 --port-base-offset 500 --trace-dir traces/smoke_batch_react
```
**FAILED:** both tasks → `ERROR: 'NoneType' object has no attribute 'evaluate'`.
Debugged: not concurrency — same error on both, and the agents had actually run fine
(T077's SubAgent even emitted `<FINAL_ANSWER>35532</FINAL_ANSWER>`). Traced to the
grader's unguarded `judge.evaluate` under `--no-judge` (Hazard 6). **Fix:** enable the
judge in the config (sonnet endpoint), drop `--no-judge`. No code edited.

### 3c. react 2-task concurrent batch — judge enabled (re-run)
```
CLAWEVAL_AORCHESTRA_RUNTIME=react python -m claw_eval.cli batch --range 77-78 \
  --harness aorchestra --config config_concurrency_smoke.yaml \
  --parallel 2 --port-base-offset 500 --trace-dir traces/smoke_batch_react2
```
| task | score | completion | robustness | errors |
|------|-------|-----------|-----------|--------|
| T077 | 0.20  | 0.00      | 1.00      | none   |
| T078 | 0.64  | 0.55      | 1.00      | none   |

Avg 0.42 (matches the weak react baseline, ~0.28–0.47). **0/2 errors.** Distinct traces
(`-t0-p500` / `-t0-p550`), distinct raw dirs. Judge output verified per-task: T077's judge
discusses "department name / spending amount", T078's judge discusses "March 1969 / spread
values / MMYYYY" — no leak. Both OCR services started and stopped cleanly; no
connection-refused / address-in-use.

### 3d. pi 2-task concurrent batch (the positive-signal test)
```
CLAWEVAL_AORCHESTRA_RUNTIME=pi python -m claw_eval.cli batch --range 77-78 \
  --harness aorchestra --config config_concurrency_smoke.yaml \
  --parallel 2 --port-base-offset 500 --trace-dir traces/smoke_batch_pi
```
| task | score | completion | robustness | wall | errors |
|------|-------|-----------|-----------|------|--------|
| T077 | **0.92 PASS** | 0.91 | 1.00 | 60s  | none |
| T078 | 0.72 FAIL     | 0.65 | 1.00 | 300s | none |

Avg 0.82, pass@1 1/2. T077 = 0.92 matches the pi baseline (~0.95). **True parallelism
observed:** T077 finished at 60s while T078 kept running to 300s in the other worker.

**Cross-contamination check (decisive):**
- T077 trace contains `36,080` + `Defense`, and **0** occurrences of T078's `031969`.
- T078 trace contains `031969` + `March 1969` + `yield spread`, and **0** occurrences of
  T077's `36080` / `Defense`.
- Distinct trace files + raw dirs (p500 vs p550). Zero connection/port errors in either.

(Note: an early grep flagged `9682` in T078's trace; on inspection it was a substring of a
tool_use UUID `0f91fca3599b4c69ab2ee79682f8d599`, **not** a port. No port anomaly.)

**Conclusion of smoke:** under 2-way concurrency at offset 500, each task got its own OCR
document, its own port, its own answer, and its own judge evaluation. No corruption in
either runtime.

---

## 4. Verdict

**The AOrchestra harness is SAFE to run a concurrent sonnet rollout** (5-task, then
larger), in both `react` and `pi` runtimes, subject to these precautions:

1. **MUST pass `--port-base-offset`** when other batch jobs may be running concurrently
   (this investigation used 500; OpenClaw investigation uses 0). Within a single batch job
   the per-slot stride already prevents intra-job port collisions; the base offset only
   separates *different* jobs.
2. **MUST NOT pass `--no-judge`** for tasks with an `llm_judge` scoring component (most
   officeqa/communication tasks) — it crashes grading (Hazard 6). Provide a judge endpoint
   instead. Using the same sonnet endpoint for the judge works.
3. **Set `CLAWEVAL_AORCHESTRA_RUNTIME`** (and the `CLAWEVAL_LLM_*` creds + `AORCHESTRA_ROOT`)
   in the shell **before** launching the CLI — fork inheritance carries them into workers.
4. **Max safe `--parallel`** at offset 500 is ~463 (CLI guard at `cli.py:1424`); a 5-task
   `--parallel 5` tops out at port 9829, far below the 32768 ephemeral floor. Parallelism
   is effectively bounded by API rate limits / RAM, not ports.

---

## 5. Recommended command for the upcoming 5-task sonnet rollout

```bash
cd /data2/ruanjianhao/claw-eval && source .venv/bin/activate
export AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra
export CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1
export CLAWEVAL_LLM_API_KEY=sk-...          # sonnet via deepwisdom
export CLAWEVAL_LLM_MODEL=claude-sonnet-4-5
export CLAWEVAL_AORCHESTRA_RUNTIME=pi       # or react
export no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1

python -m claw_eval.cli batch \
  --range <pick-5-host-mode-tasks> \        # e.g. officeqa OCR tasks T077-T085 (no SANDBOX tools)
  --harness aorchestra \
  --config config_concurrency_smoke.yaml \  # judge wired to sonnet; or a dedicated rollout config
  --parallel 5 \
  --port-base-offset 500 \
  --trace-dir traces/rollout_aorchestra_pi
```
- Pick 5 **host-mode** tasks (only HTTP-service tools, no Bash/Read/Write/etc.) unless you
  also pass `--sandbox` + a built sandbox image. The officeqa OCR family (T076–T085) is the
  safest pool: all single-`ocr_extract_text` tasks.
- Add `--trials N` for multi-trial pass^k if desired (each trial is sequential within a
  task; the parallelism is across tasks).
- `--port-base-offset 500` keeps clear of the OpenClaw investigation's offset-0 range.

---

## 6. Open issues / follow-ups

1. **Grader `--no-judge` crash (Hazard 6).** `officeqa` graders (e.g.
   `tasks/T077_.../grader.py:86`, `_call_judge`) call `judge.evaluate(...)` without an
   `if judge is None` guard, so `--no-judge` raises `NoneType has no attribute evaluate`
   instead of skipping the judge component. Pre-existing; not concurrency-related. Fix
   would be a guard in `_call_judge` (return 0 / renormalize weights when judge is None),
   ideally in the shared `officeqa_reward` / `AbstractGrader` path so all officeqa tasks
   benefit. **Out of scope for this concurrency investigation; did not modify code.**
2. **Empty `step_log.jsonl` in raw dirs.** Both smoke runs produced 0-line
   `step_log.jsonl` in the `_raw/` dirs. The trace JSONL itself is complete and grades
   correctly, so this is cosmetic, but worth checking whether the bridge `env.step_log()`
   capture is wired for the host-smoke path. Not a concurrency issue.
3. **Duplicate `patched_llms_config` definition.** `_bridge/model_config.py` defines
   `patched_llms_config` twice (lines 76 and 93, identical bodies). Harmless (second wins),
   but a lint smell — worth deleting one copy. Not concurrency-related.
4. **react quality on officeqa is low** (T077 0.20, vs pi 0.92). Expected from prior
   baselines; pi is the runtime to use for the OCR-heavy rollout. Not an infra issue.
5. **Larger-scale validation.** This was 2-way concurrency. Before a big run, a quick 5-way
   `--parallel 5` smoke would confirm slot recycling under more tasks-than-workers, though
   the architecture (per-slot offsets + process isolation) gives no reason to expect new
   failures.

### Artifacts (smoke run trace dirs)
- `traces/smoke_single_react/`        — single react sanity
- `traces/smoke_batch_react/`         — first (failed `--no-judge`) react batch — the debug case
- `traces/smoke_batch_react2/`        — react 2-task concurrent, judge on (0.20 / 0.64)
- `traces/smoke_batch_pi/`            — pi 2-task concurrent, judge on (0.92 / 0.72)
- `config_concurrency_smoke.yaml`     — throwaway config (model + judge → sonnet)
