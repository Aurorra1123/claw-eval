# OpenClaw harness — concurrency (parallel batch) readiness

Investigation date: 2026-06-26. Goal: confirm the `openclaw` harness in claw-eval
runs correctly under concurrent (`batch --parallel N`) execution before scaling a
sonnet-based rollout. Verdict up front: **the OpenClaw harness was NOT
concurrency-safe in batch mode (in fact it was completely broken in batch mode);
a minimal fix was applied and the harness is now safe.** Details, evidence, and the
debugging trail below.

---

## 1. Concurrency architecture (how batch parallelism works)

`cli.py :: cmd_batch` (~line 1263) drives parallelism with a
`ProcessPoolExecutor(max_workers=workers)` (`--parallel`, default 4). It is a
**container-per-task** model:

- Tasks are discovered from `--tasks-dir` and filtered by `--filter` / `--tag` /
  `--range`.
- A **slot pool** of size `workers` is maintained (`available_slots`, cli.py
  ~1413). Each in-flight task occupies one slot; slots are recycled on
  completion. Slot `s` maps to a **port offset**
  `offset = port_base_offset + s * _STRIDE` where `_STRIDE = 50` (cli.py
  ~1422-1435). `--port-base-offset` lets multiple independent batch jobs coexist
  (e.g. AO investigation uses 500, this investigation uses 0).
- Each worker runs `_run_single_task(...)` in a child process (cli.py ~870).
  Inside it:
  - `task.apply_port_offset(offset)` shifts **every** declared service port and
    **every** `tool_endpoints[*].url` by the same offset (task.py:138). Mock
    services and the bridge plugin that points at them move in lockstep.
  - `run_id = f"{task_id}-t{i}-p{port_offset}"` — unique per (task, trial,
    offset) (cli.py:997).
  - For `--harness openclaw --sandbox`: a sandbox container is started, the
    bridge plugin is generated on the host and installed **inside** the container
    via `docker exec`, OpenClaw runs via `docker exec openclaw agent ...`, and
    the trace is translated from the container's `session.jsonl` + the bridge
    traffic log.

OpenClaw containers run with `network_mode="host"` (design §3.7): the bridge
plugin executes inside the container (via `docker exec openclaw`) and must reach
host mock services at `localhost:<offset port>`; the host must also probe the
in-container sandbox server through `localhost`. Host networking is therefore
mandatory for this harness — which makes the sandbox server's host port a
concurrency-relevant resource (see hazard #2).

Container naming: `claw-agent-{run_id}` (sandbox_runner.py:119). Cleanup:
`stop_container` force-removes by handle in a `finally` (cli.py:1026); a
`cleanup_all` by label (`app=claw-eval`) exists for crash recovery.

---

## 2. Hazards checked (the 5 items) — verdicts + evidence

### #1 Container naming under concurrency — SAFE
Container name = `claw-agent-{run_id}` (`sandbox_runner.py:119`) and
`run_id = "{task_id}-t{i}-p{port_offset}"` (`cli.py:997`). Because each worker slot
has a distinct `port_offset`, names never collide across concurrent tasks.
**Observed:** `claw-agent-T076_officeqa_defense_spending-t0-p0` and
`claw-agent-T077_officeqa_highest_dept_spending-t0-p50` ran simultaneously
(`docker ps` during the run). No "name already in use" error.

### #2 Sandbox-server port allocation — WAS UNSAFE → FIXED
With `network_mode="host"`, `start_container` ignored docker port publishing and
bound the sandbox server to a **fixed** `sandbox_config.sandbox_port` (8080)
(`sandbox_runner.py:131-141`, `config.py:73`). Two concurrent host-network
containers would both try to bind host:8080 → collision. The in-container server
port is also baked into the image CMD (`Dockerfile.openclaw:73`,
`--port 8080`), so it cannot be changed without an override.
**Fix:** `start_container` now accepts `sandbox_port`, overrides the container
**command** to `python3 /opt/sandbox/server.py --port <p> --host 0.0.0.0` (no
image rebuild), and `_run_single_task` passes `sandbox_port = 8080 + port_offset`
per worker. **Observed after fix:** T076 sandbox server on host:8080, T077 on
host:8130, both `/health=200` simultaneously; `docker inspect` confirmed the
CMD override and `NetworkMode=host` for each.

### #3 Bridge case_dir / state_dir collisions — SAFE
- Bridge plugin id `claweval-bridge-<task_id>-<run_id>`
  (`generator.py:140-148`) — unique per run.
- Traffic log `case_dir/bridge_traffic.jsonl` where
  `case_dir = trace_dir/{task_id}_{run_id}_raw/raw` (`openclaw.py:249-252`,
  `generator.py:497`) — unique per run.
- OpenClaw isolation dirs `OPENCLAW_STATE_DIR` / `OPENCLAW_HOME` / `HOME` point
  at `case_dir/openclaw_state` and `case_dir/openclaw_home`
  (`generator.py:472-477, 518-524`; `_openclaw_container.py:149-153`). No shared
  global `~/.openclaw` is touched — each task installs the bridge into its own
  per-case state dir.
**Observed:** the two tasks' bridge logs were distinct files and each contained
exactly its own task's OCR call — T076 → `localhost:9120`, T077 →
`localhost:9171`. No cross-talk.

### #4 Mock service ports (OCR etc.) consistent with bridge tool_endpoints — SAFE
`task.apply_port_offset(offset)` shifts `services[*].port` /
`health_check` / `reset_endpoint` AND `tool_endpoints[*].url` with the **same**
offset in one pass (`task.py:155-164`). The bridge plugin source bakes in
`tool_endpoints[*].url` verbatim, so the plugin always points at the same
offset port the mock service binds.
**Observed:** T076 OCR service started on 9120 (`9120 + 0`), T077 on 9171
(`9121 + 50`); the matching bridge logs hit exactly those ports.

### #5 Docker resource limits / GPU — SAFE (CPU/LLM only); recommend --parallel ceiling
OpenClaw is LLM-driven via the sonnet API; the container runs only the sandbox
HTTP server + ad-hoc `openclaw`/`node` subprocesses. **No GPU usage** — confirmed
no docker GPU jobs running (the only pre-existing container is an unrelated
`paper_eval` job; the GUI-Owl/vllm GPU jobs are bare processes, not docker).
Per-container limits: `mem_limit=4g`, `nano_cpus=2.0 CPU` (`config.py:71-72`).
Memory is the main scaling constraint: each container reserves up to 4g and the
OpenClaw + npm/tsc + node toolchain plus the host-side mock services add load.
See recommended ceiling in §5.

---

## 3. The bug + the fix (debugging process)

### What failed (run #1 — `--no-judge`, before fix)
First 2-task concurrent batch (`--range 76-77 --parallel 2 --harness openclaw
--sandbox --port-base-offset 0`) finished in ~3s with **both tasks ERROR**:

```
bridge install failed running ['docker','exec', ... ,'-w',
 '/tmp/.../T076_..._raw/raw/bridge_plugin', '<container_id>','npm','install']:
 rc=127, stderr=''
```

`rc=127` from `docker exec ... -w <path> npm install` means the working directory
did not exist inside the container. Root cause: `_run_single_task` started the
container with a bare `sandbox_runner.start_container(run_id=run_id)` (cli.py:998,
pre-fix) — it did **not** apply the OpenClaw-specific setup that the single-task
`cmd_run` path does (cli.py:462-472):
- no `network_mode="host"` → the bridge `docker exec` could not reach host mock
  services anyway, and
- no `volumes={case_dir: case_dir}` → the host-side `bridge_plugin` dir was not
  visible inside the container, so the `-w .../bridge_plugin` cwd was missing.
- no `CLAWEVAL_BRIDGE_LOG` extra_env.

So **batch mode never worked for openclaw at all** — concurrent or serial. The
`cmd_run` (single `run` subcommand) path had the setup; the batch worker was a
divergent copy that missed it.

### The fix (minimal, two files)
1. `runner/sandbox_runner.py :: start_container` — added `sandbox_port: int|None`
   kwarg. When set it (a) overrides the container CMD to bind that port (so we do
   not need to rebuild the 1.54GB image whose CMD hardcodes 8080), (b) publishes
   that port in bridged mode, (c) is returned as the handle's host port. Default
   `None` preserves the old behaviour (config's 8080). `_get_mapped_port` gained
   an optional `container_port` arg.
2. `cli.py :: _run_single_task` — for `harness_name == "openclaw"`, mirror
   `cmd_run`'s setup: `network_mode="host"`, mount the per-task case_dir at the
   same path, set `CLAWEVAL_BRIDGE_LOG`, and pass a **unique**
   `sandbox_port = 8080 + port_offset` so concurrent host-network containers do
   not collide on 8080.

The fix is additive: non-openclaw harnesses and the single-task `run` path keep
their previous behaviour (they pass no `sandbox_port`, so it defaults to 8080).

### Verification
- `pytest tests/ -p no:quadrants` → **94 passed, 4 skipped** (e2e gated on
  RUN_E2E/docker), 0 failures. No regression.
- Re-ran the 2-task batch with judge disabled: bridge install succeeded, both
  containers ran OpenClaw to completion and produced valid distinct traces
  (346 KB / 448 KB, each with one `ocr_extract_text` bridge call). New error
  surfaced only at the **grading** step: `'NoneType' object has no attribute
  'evaluate'` — the officeqa graders call `judge.evaluate(...)` unconditionally
  (`tasks/T076.../grader.py:74`, `T077.../grader.py:86`), so `--no-judge` (which
  sets `judge=None`) crashes them. This is a grading-config issue, **not** a
  concurrency or harness issue — the rollouts themselves succeeded.
- Re-ran the 2-task batch with the judge **enabled** (sonnet as judge, same
  deepwisdom creds): both tasks PASS — see §4.

---

## 4. Smoke run results

Environment: isolated venv at `/tmp/oc_venv` (project editable + `[mock,sandbox,dev]`
extras). Tasks T076 + T077 (officeqa OCR, mock-service-only, known-good container
baselines). Image `claw-eval-agent-openclaw:latest` (ad48d5b1707c, 1.54 GB).

Exact command (judge-enabled, final smoke):
```
python -m claw_eval.cli batch \
  --tasks-dir tasks --range 76-77 --parallel 2 \
  --harness openclaw --sandbox \
  --port-base-offset 0 --trials 1 \
  --config <sonnet-config-with-judge-enabled> \
  --model claude-sonnet-4-5 \
  --base-url https://newapi.deepwisdom.ai/v1 --api-key <key> \
  --trace-dir <out>
```

Concurrency isolation observed (all simultaneous, no collisions):

| Task | Container name | Sandbox server (host) | Mock OCR port | Bridge log target |
|------|----------------|-----------------------|---------------|-------------------|
| T076 | `claw-agent-T076_officeqa_defense_spending-t0-p0`  | `localhost:8080` | `9120` (9120+0)  | `localhost:9120` |
| T077 | `claw-agent-T077_officeqa_highest_dept_spending-t0-p50` | `localhost:8130` | `9171` (9121+50) | `localhost:9171` |

`docker inspect` confirmed `NetworkMode=host` + the per-port CMD override + a
unique per-task volume mount for each. Bridge traffic logs were distinct files,
each containing only its own task's OCR request — no cross-talk.

Scores (judge enabled):

| Task | task_score | completion | robustness | communication | safety | tokens (in/out) | wall |
|------|-----------:|-----------:|-----------:|--------------:|-------:|-----------------|-----:|
| T076 | **0.99 PASS** | 0.99 | 1.00 | (judge) | 1.00 | 48710 / 460 | 29.5s |
| T077 | **0.95 PASS** | 0.94 | 1.00 | (judge) | 1.00 | 40417 / 414 | 29.1s |

Batch aggregate: avg 0.973, pass^1 2/2, pass@1 2/2, **errored 0/2**. T077's 0.95
matches the prior Phase-3 container baseline (~0.95) exactly. No "name already in
use", no "port already allocated", no "address already in use". Containers were
force-removed cleanly (`docker ps -a` showed no leftover `claw-agent-*`).

Note: OpenClaw container runs were faster than expected here (~29s each, not the
2-5 min anticipated) because these OCR tasks are a single tool call + short
reasoning; heavier tasks will take longer.

---

## 5. Verdict & recommendations

**The OpenClaw harness is SAFE for a concurrent sonnet rollout — *with the fix in
this change applied*.** Without the fix, batch mode is unusable for openclaw
(bridge install fails immediately). With it, container names, sandbox-server host
ports, mock-service ports, bridge logs, and OpenClaw state dirs are all uniquely
isolated per worker, and a 2-task concurrent batch produced correct, baseline-
matching scores with zero corruption.

### Required flags / precautions for the rollout
- `--harness openclaw --sandbox` (the `--sandbox` gate is enforced; openclaw on
  host is refused for production).
- A config with the **judge enabled** (officeqa graders call `judge.evaluate`
  unconditionally — `--no-judge` crashes grading for these tasks). Point the
  judge at the same sonnet creds, or a dedicated judge model.
- `--port-base-offset 0` is fine for this job (the AO investigation uses 500;
  ranges stay disjoint). If running multiple openclaw batch jobs at once, give
  each a distinct `--port-base-offset` with enough gap (>= `parallel * 50`).

### Recommended `batch` command for the upcoming 5-task sonnet rollout
```
python -m claw_eval.cli batch \
  --tasks-dir tasks --range <pick 5 tasks> \
  --parallel 5 --harness openclaw --sandbox \
  --port-base-offset 0 --trials 1 \
  --config <sonnet-config-judge-enabled> \
  --model claude-sonnet-4-5 \
  --base-url https://newapi.deepwisdom.ai/v1 --api-key <key> \
  --trace-dir <out>
```
`--parallel 5` → sandbox ports 8080,8130,8180,8230,8280 and mock ports
9100+offset; all clear of each other.

### Recommended max --parallel
- **Port headroom:** sandbox port = `8080 + slot*50`; it reaches the mock-service
  range (9100+) at slot ~20. Mock services live at `9100-9129 + offset`. To stay
  clear, keep `--parallel <= ~16` with `--port-base-offset 0`. (Matches the
  README's `--parallel 16` example.)
- **Memory:** each container reserves up to 4g (`config.py`). On a shared box
  also running GPU/vllm jobs, 4-8 parallel is a conservative start; 16 only if
  there is comfortably >64-80 GB free RAM. Scale up gradually (5 → 8 → 16) and
  watch `docker stats` / host free memory.
- **Docker daemon / FDs:** 16 short-lived containers per wave is well within
  default daemon limits; nothing special needed. The slot pool keeps at most
  `parallel` containers alive at once.

**Suggested first scale step:** `--parallel 5` (the planned 5-task run), then
`--parallel 8`, monitoring memory, before going to 16.

---

## 6. Open issues / follow-ups

1. **Grader + `--no-judge`:** the officeqa graders (and likely other
   `llm_judge`-component graders) call `judge.evaluate(...)` without a `None`
   guard, so `--no-judge` raises `AttributeError` at grading. Not a blocker for
   the rollout (run with the judge enabled), but a latent footgun. Consider a
   guard in the graders or a CLI warning. Not fixed here (out of scope).
2. **Sandbox-port scheme is linear (`8080 + offset`).** It collides with the
   mock-service range only beyond ~16-20 workers. If a future rollout needs
   higher parallelism, give the sandbox server its own offset band well separated
   from the 9100 mock range (e.g. a dedicated base like 8000 + slot, or move mock
   services up). Documented, not changed.
3. **Code divergence between `cmd_run` and `_run_single_task`.** The two paths
   each hand-roll the openclaw container setup; this investigation fixed the batch
   copy to match. A shared helper would prevent the two from drifting again.
4. The fix overrides the container CMD at `docker run` time rather than rebuilding
   the image. If the image's sandbox `server.py` CLI ever changes its `--port`
   flag, the override string in `sandbox_runner.py` must track it.
