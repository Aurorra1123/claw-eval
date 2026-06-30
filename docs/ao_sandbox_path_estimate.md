# AOrchestra sandbox/container path — work-estimate & task breakdown

**Date:** 2026-06-29
**Scope:** scoping only — reading code, no implementation, no re-runs, no docker/GPU.
**Goal:** estimate the work to let the AOrchestra (AO) harness run SANDBOX_TOOLS
(`Bash/Read/Write/Edit/Glob/Grep/BrowserScreenshot/ReadMedia/Download`) so AO can
join the toolset-matched fairness comparison against the Claude-Sonnet-4.5
baseline (which used `Read`+`Bash`+`grep`+`sed` to extract document tables on
office_qa tasks).

Companion to `docs/sandbox_tools_feasibility.md` (which established *whether* each
harness can enable sandbox tools and *how many* tasks are affected). This doc is
the **engineering scoping** of the AO-specific gap only.

**TL;DR.** The env-level sandbox dispatch and the whole container/server stack
already exist and are already reused by claweval/OpenClaw. The CLI gate already
starts the container and hands AO a `sandbox_handle`. The only meaningful gap is
the body of `AOrchestraHarness._run_container`, plus one design decision about
**how AO discovers which sandbox tools to expose** (AO reads `task.tools`;
claweval *appends* the full 9-tool set in its loop). With the simplest scoping
(task-declared tools, mirroring OpenClaw), this is **Small–Medium**: wire ~5
existing functions into `_run_container`. The "full appended set like claweval"
option adds a small env change and is still **Medium**.

---

## 1. What already exists (the building blocks)

### 1.1 Env-level sandbox dispatch — **DONE**
`ClawEvalEnv._build_actions` (`src/claw_eval/harnesses/aorchestra/_bridge/env.py:212-233`)
already branches on `tool.name in SANDBOX_TOOL_NAMES` (`env.py:216`) and calls
`make_sandbox_action(tool, self._sandbox_url, self._step_log, agent_role=...)`
(`env.py:218-221`). When `sandbox_url` is set, it builds a real HTTP action.

`make_sandbox_action` (`_bridge/actions.py:168-197`) maps the tool name to the
sandbox-server path via `SANDBOX_ENDPOINTS = dict(SandboxToolDispatcher._PATH_MAP)`
(`actions.py:50`, reusing the canonical claweval map) and produces a
`make_http_action` that POSTs to `sandbox_url + endpoint_path` — i.e. it reuses
the same plain-HTTP action machinery as mock-service tools (`actions.py:191-197`).
**No new dispatch code is needed.**

`ClawEvalEnv` already implements the full AOrchestra `Environment` ABC
(`step()` env.py:140, `get_basic_info()` env.py:114, `get_action_space_for(role)`
env.py:85, `tool_schemas()` env.py:99), and the constructor already takes
`sandbox_url` (`env.py:49`). The host path constructs it with `sandbox_url=None`
(`harness.py:172`); the container path just needs to pass `sandbox_handle.sandbox_url`.

### 1.2 Runtimes need **no change**
Both AO sub-agent runtimes are tool-agnostic — they dispatch whatever is in the
action space and never special-case sandbox tools:
- `ReActRuntime.run` (`/data2/ruanjianhao/AOrchestra/aorchestra/runtime/react_runtime.py:51`)
  drives the standard `Runner` over the action-space text.
- `PiRuntime.run` (`pi_runtime.py:97`) reads `spec.tool_schemas[name]`
  (`pi_runtime.py:300`) and dispatches via `env.step(action)` (`pi_runtime.py:359`).
  `tool_schemas` is already populated for every task tool by
  `_build_aorchestra_tool_schemas(env)` (`_runner.py:264-274`, passed into
  `DelegateTaskTool(tool_schemas=...)` at `_runner.py:407`). Sandbox tools, once
  in `task.tools`, get schemas for free because `tool_schemas()` iterates all
  `task.tools` (`env.py:107`).

So this is purely an **env + container-wiring** question, not a runtime change.

### 1.3 MainAgent/SubAgent driver passes tools through automatically
`_runner.run_one_task` (`_runner.py:338`) builds tool sets from the env:
`main_claweval_tools = env.get_action_space_for("main")` (`_runner.py:378`) and
`sub_claweval_tools = env.get_action_space_for("sub")` (`_runner.py:379`). If the
env was constructed with a `sandbox_url`, **both** main and sub action spaces
already include the sandbox actions (because `_build_actions` builds them for any
role). The SubAgent runner is fed `[*sub_claweval_tools, CompleteTool()]`
(`_runner.py:386-388`) and MainAgent gets `subagent_tools=[*sub_claweval_tools, ...]`
(`_runner.py:438`). **No change to `run_one_task` is needed** — sandbox tools flow
to both agents the moment the env has a `sandbox_url`.

### 1.4 SandboxRunner — container lifecycle, **DONE & reused**
`src/claw_eval/runner/sandbox_runner.py`:
- `start_container(run_id, network_mode, volumes, extra_env, sandbox_port)`
  (`sandbox_runner.py:76`) launches the image, overrides CMD to bind the port,
  waits `/health`, returns `ContainerHandle(container, host_port, run_id, sandbox_url)`
  (`sandbox_runner.py:169-174`).
- `inject_files(handle, task, task_dir)` (`sandbox_runner.py:301`) pushes
  `task.sandbox_files` (or `task.environment.fixtures` fallback) into `/workspace/<rel>`
  via the server's `/write` and `/write_b64` (`sandbox_runner.py:257-277`).
- `inject_grader_files` (`sandbox_runner.py:332`) and `stop_container`
  (`sandbox_runner.py:176`) round out the lifecycle.

### 1.5 The CLI gate already starts the container for AO — **DONE**
The biggest building block: `cli.py` **already** treats AO + `--sandbox` as a
container run and hands AO a `sandbox_handle`:
- `cmd_run` (`cli.py:449-507`): when `sandbox_mode`, instantiates
  `SandboxRunner(cfg.sandbox, image=sandbox_image)` (`cli.py:458`), starts the
  container (`cli.py:492`), `runner.inject_files(...)` (`cli.py:494`), then calls
  `harness.run(..., sandbox_handle=handle, sandbox_tools=True)` (`cli.py:498-507`),
  then `inject_grader_files` + `_collect_env_snapshot` + `stop_container`
  (`cli.py:511-523`).
- `cmd_batch`/worker (`cli.py:1015-1085`): same pattern under concurrency.
- The AO host-mode gate (`cli.py:434-447`) only refuses sandbox tools **without**
  `--sandbox`. With `--sandbox` it falls through to the container path above.

So for the single-run and batch paths, **the CLI already does steps (a) start
container, (b) inject fixtures, (e) snapshot, (f) teardown** around AO. The
harness's `_run_container` only has to do the in-between (construct the env with a
`sandbox_url`, run the loop, collect trace/audit).

### 1.6 The sandbox-server image is the same one claweval uses — **DONE**
`claw-eval-agent:latest` (`Dockerfile.agent`) is a *sandbox-server-only* image
(`COPY src/claw_eval/sandbox/ /opt/sandbox/`, `ENTRYPOINT .../server.py --port 8080`,
`WORKDIR /workspace`). AO runs its agent loop **on the host** (it's a Python
library), exactly like claweval — only the sandbox server lives in the container.
So AO uses the **default image**, NOT `claw-eval-agent-openclaw:latest`. The CLI
only overrides to the OpenClaw image for `--harness openclaw` (`cli.py:456-457`);
for AO the default `cfg.sandbox.image = "claw-eval-agent:latest"` (`config.py:69`)
is correct.

### 1.7 Fixtures land where the baseline read them — **DONE**
`inject_files` writes to `/workspace/<rel_path>` (`sandbox_runner.py:257`). office_qa
tasks declare e.g. `sandbox_files: [fixtures/ocr/treasury_bulletin_1970_06.txt,
fixtures/pdf/treasury_bulletin_1970_06.pdf]` (`tasks/T078_officeqa_max_yield_spread/task.yaml:50-52`),
so they materialise at `/workspace/fixtures/ocr/...` — the path the baseline agent
`Read`/`grep`/`sed`'d. The OCR mock reads fixtures from the host task dir
independently; the two are separate copies and don't conflict.

---

## 2. What's missing (the concrete gap list)

### Gap A (the real work) — implement `AOrchestraHarness._run_container`
`harness.py:231-245` raises `NotImplementedError`. It must mirror the working
`_run_host_smoke` (`harness.py:131-225`) with one substitution: pass the
`sandbox_url` into the env. Step-by-step, with reuse status:

| Step | Action | New code or existing call? |
|---|---|---|
| (a) start container | **already done by the CLI** (`cli.py:492`/`1041`); the handle arrives as `sandbox_handle` | existing (caller) |
| (b) inject fixtures | **already done by the CLI** (`runner.inject_files`, `cli.py:494`/`1066`) | existing (caller) |
| (c) construct env | `ClawEvalEnv(task, sandbox_url=sandbox_handle.sandbox_url)` | existing class; the only AO-side change vs host is `sandbox_url=...` instead of `None` (`harness.py:172`) |
| (d) run the loop | `asyncio.run(_runner.run_one_task(task, env, cfg, case_dir=case_dir, sandbox_url=sandbox_handle.sandbox_url))` | existing function (`_runner.py:338`), unchanged |
| (e) write step_log | `self._write_step_log(env, step_log_path)` | existing helper (`harness.py:259`) |
| (f) audit | `self._collect_audit(task, services_ctx)` | existing helper (`harness.py:269`) |
| (g) env_snapshot | leave to caller (the CLI calls `_collect_env_snapshot` + `inject_grader_files` after `harness.run`, `cli.py:511-520`), OR mirror host's `inject_grader_files_host`/`collect_workdir_snapshot` block — **but for the container, snapshot must come from the sandbox server, not the host workdir** | small decision (see Risk R3) |
| (h) translate trace | `translate_aorchestra(...)` | existing (`harness.py:209`), unchanged |
| (i) teardown | **already done by the CLI** (`runner.stop_container`, `cli.py:523`) | existing (caller) |

In practice `_run_container` is ~90% a copy of `_run_host_smoke` minus the
host-mode SystemExit gate (`harness.py:148-157`), with `sandbox_url` threaded into
the env + `run_one_task`, and with the env_snapshot block removed/deferred (the
CLI owns it for the container path). This is the bulk of the work and it is small.

### Gap B (design decision) — how does AO learn *which* sandbox tools to expose?
This is the one genuine divergence from claweval and the crux of the fairness goal:

- **claweval** does NOT rely on `task.tools`. Its loop *appends* the full 9-tool
  `SANDBOX_TOOLS` set whenever `sandbox_tools=True` (`runner/loop.py:261-268`:
  `task_tools = list(task.tools) + sandbox_tool_list`). That is exactly how the
  baseline got `Bash`/`Read`/`grep` on office_qa tasks whose `task.yaml` only
  declares `ocr_extract_text`.
- **AO** builds actions **only from `task.tools`** (`env.py:215`). It never
  appends `SANDBOX_TOOLS`. So implementing Gap A alone gives AO sandbox tools
  **only on tasks whose `task.yaml` already declares them** — which office_qa
  tasks do **not**. To match the baseline, AO needs ONE of:
  - **B1 (cheap, matches OpenClaw's story):** edit each office_qa `task.yaml`
    `tools:` list to declare `Read`/`Bash`/`Grep`. This changes the task contract
    for *all* harnesses and is out of scope under the feasibility doc's
    constraints — a deliberate, separate decision.
  - **B2 (matches claweval exactly):** add a small "append SANDBOX_TOOLS" step so
    that when the container path runs (or `sandbox_tools=True`), AO injects the
    full 9-tool set the way `loop.py` does. Concretely: build the extra
    `ToolSpec`s from `get_sandbox_tools()`/`SANDBOX_TOOLS` (dedup against
    `task.tools` by name) and have `ClawEvalEnv` build sandbox actions for them
    too. This is the only behavioural change beyond Gap A, and it's what makes AO
    *actually* baseline-comparable without touching task.yaml.

  **Recommendation:** B2. It is the only option that reproduces the baseline
  setup as-is and keeps the task definitions untouched. It is a ~20-40 line change
  (a helper that returns `task.tools + appended SANDBOX_TOOLS`, plumbed into
  `ClawEvalEnv._build_actions` behind a "sandbox tools enabled" flag derived from
  `sandbox_url is not None`).

### Gap C (tiny) — pass `sandbox_tools`/snapshot intent through
`AOrchestraHarness.run` accepts `sandbox_tools: bool` (`harness.py:103`) but
ignores it (the dispatch keys only off `sandbox_handle`, `harness.py:110`). If B2
is chosen, `_run_container` should honour the same "append full set" semantics
the CLI signals via `sandbox_tools=True` (`cli.py:506`). Trivial.

---

## 3. Risk / impedance points

- **R1 — `task.tools` vs appended set (the main one).** Covered as Gap B. Without
  B2 (or B1's task.yaml edits), implementing `_run_container` does NOT make AO
  baseline-comparable on office_qa — it would expose zero sandbox tools on those
  tasks. This is the make-or-break point for the fairness goal.

- **R2 — networking under concurrency.** The CLI sets
  `network_mode="host"`/`sandbox_port`/`volumes` start_kwargs **only for
  `--harness openclaw`** (`cli.py:481-491`, batch `cli.py:1029-1040`). For AO the
  start_kwargs are just `{"run_id": run_id}`, so the container runs in **bridged**
  mode with a dynamically-mapped host port (`sandbox_runner.py:157,164`). That is
  correct for AO (agent loop on host → mock-service HTTP goes host→host; sandbox
  HTTP goes host→container via the mapped port — no host networking needed). **But
  under `cmd_batch` parallelism, bridged mode is fine (dynamic ports avoid
  collisions), whereas claweval/OpenClaw concurrency was validated with host-net +
  `sandbox_port` offset.** Worth a quick check that the AO batch path doesn't need
  a per-worker `sandbox_port`; bridged dynamic mapping should sidestep it, but it's
  the one untested concurrency wrinkle.

- **R3 — env_snapshot source for the container path.** Host `_run_host_smoke`
  snapshots the host `task_dir` (`harness.py:200-206`). For the container, files
  the agent wrote live **inside the container** (`/workspace`), so the snapshot
  must come from the sandbox server, not the host. The CLI already does this for
  the container path (`_collect_env_snapshot(handle.sandbox_url, task)` after
  `harness.run`, `cli.py:520`), so `_run_container` should **return
  `env_snapshot=None`** and let the CLI collect it (the same contract claweval
  uses — `claweval.py:117-119` returns `env_snapshot=None` on purpose). Easy to get
  wrong if `_run_container` copies the host snapshot block verbatim.

- **R4 — no SubAgent schema mismatch.** Checked: SubAgent tools are built from the
  same `make_sandbox_action`/`make_http_action` factories and carry `parameters`
  from each `ToolSpec.input_schema`. PiRuntime reads `spec.tool_schemas` which
  already covers all `task.tools` (`env.py:99-112`). If B2 appends SANDBOX_TOOLS,
  `tool_schemas()` must also iterate the appended set (same one-line consideration
  as Gap B). No deeper schema-translation work — sandbox tools use plain JSON
  Schemas (`sandbox_tools.py:11-302`), no oneOf/allOf/$ref.

- **R5 — param-name translation.** claweval's `SandboxToolDispatcher._translate_payload`
  (`sandbox_dispatcher.py:138-160`) maps client param names to server ones (e.g.
  `file_path`→`path`, `timeout`ms→`timeout_seconds`). AO's `make_sandbox_action`
  POSTs the LLM's kwargs **verbatim** to the server (`actions.py:143-163` via
  `make_http_action`) — it does **not** apply that translation. The sandbox server
  must accept the Claude-Code-style names directly, or AO sandbox calls will fail
  with the SANDBOX_TOOL schemas as written (`Read` sends `file_path`, server
  `/read` expects `path` per `dispatcher` translation). **This is a real
  impedance point**: either (i) the server already accepts both (its local
  handlers do — `_handle_file_read` reads `file_path` OR `path`,
  `sandbox_dispatcher.py:336` — but the *HTTP server* in `src/claw_eval/sandbox/`
  was not read here), or (ii) AO needs a small payload-translation shim mirroring
  `_translate_payload`. **Verify the sandbox server's accepted param names before
  estimating B2 as done** — this is the most likely hidden 1-pointer.

- **R6 — host-mode gate stays.** The SystemExit(2) gates (`harness.py:148-157`,
  `cli.py:434-447`) are correct and should remain — they only fire in host mode.
  No adjustment needed; `_run_container` is reached only with a real handle.

---

## 4. Work estimate

**Size: Small–Medium (S/M).**

Why not Large: the dispatch (`make_sandbox_action`), the container lifecycle
(`SandboxRunner`), the fixture injection, the CLI container orchestration, and the
sandbox-server image are **all already built and already exercised by
claweval/OpenClaw**. The runtimes and `run_one_task` need **zero** changes. The
literal `_run_container` body is a near-copy of the working `_run_host_smoke`.

Why not trivially Small: (1) Gap B (B2) is a real, if small, behavioural change to
make AO baseline-comparable rather than just "not crash"; (2) R5 (param-name
translation) is an unverified impedance that could add a small shim; (3) R3 (snapshot
ownership) must be handled correctly; (4) needs a smoke test.

### Task breakdown (for a plan)
1. **Implement `_run_container`** (Gap A): copy `_run_host_smoke` structure, drop
   the host SystemExit gate, construct `ClawEvalEnv(task, sandbox_url=handle.sandbox_url)`,
   thread `sandbox_url` into `run_one_task`, write step_log, collect audit, translate
   trace, return `env_snapshot=None` (R3). *~1 file, ~60 LOC, mostly mirrored.*
2. **Decide + implement tool-exposure policy** (Gap B/B2): add a helper that
   appends `SANDBOX_TOOLS` (dedup by name) when sandbox is enabled, plumb into
   `ClawEvalEnv._build_actions` + `tool_schemas()` behind a flag derived from
   `sandbox_url is not None` (or the `sandbox_tools` arg). *~20-40 LOC.*
3. **Verify/patch param-name translation** (R5): read `src/claw_eval/sandbox/server.py`;
   if it doesn't accept Claude-Code param names, add a small translation in
   `make_sandbox_action` mirroring `_translate_payload`. *0-30 LOC depending on server.*
4. **Concurrency sanity** (R2): confirm AO batch bridged-mode ports don't collide;
   add `sandbox_port` per-worker only if needed.
5. **Smoke test**: one office_qa task (e.g. T082/T084) with `--harness aorchestra
   --sandbox` end-to-end; confirm the agent issues `Read`/`Bash`/`Grep` against
   `/workspace/fixtures/...` and the trace records sandbox ToolDispatch events.

Net: **S/M** — "wire up ~5 existing pieces + one small env policy change + verify
one server contract", not "build dispatch/server from scratch."

---

## 5. Recommendation

**For the immediate fairness goal: use claweval-only for the toolset-matched
comparison; do NOT block on 補-ing AO.** Reasoning:

1. `docs/sandbox_tools_feasibility.md` already shows claweval can reproduce the
   baseline's sandbox setup **as-is today** (`--sandbox` forces the full appended
   9-tool set, `cli.py:506`/`loop.py:261-268`) and is the true apples-to-apples
   path. That comparison can be run now, on the 4 office_qa tasks (T080/82/83/84)
   + extraction subset.
2. The same doc's data shows the sandbox-tools mismatch explains only a **narrow**
   effect (≈+0.18 for OpenClaw on office_qa, concentrated in T082/T084) and that
   **AO already matches/beats baseline on average** across the 19 affected tasks
   — so the marginal analytical value of adding AO to *this specific* comparison
   is low.
3. Implementing AO sandbox is **S/M and genuinely worth doing** as harness-parity
   work (Wave 4-E was always planned), and it's cheap because the building blocks
   exist. But it is NOT on the critical path for the fairness question, and to be
   *truly* baseline-comparable it ALSO needs the tool-exposure decision (Gap B):
   either B2 (recommended, append full set) or task.yaml edits (B1) that would
   change the contract for every harness.

**Bottom line.** Run the fair comparison **now with claweval** (`--sandbox` on the
office_qa/extraction subset). Schedule **AO sandbox (`_run_container` + Gap B2 +
R5 verification)** as a separate S/M harness-parity task — worthwhile for future
AO runs and to let AO eventually join sandbox comparisons, but not a prerequisite
for answering the current baseline-fairness question.

---

*Evidence cited inline as `file:line`. No code changed; no tasks re-run. AO source
read under `/data2/ruanjianhao/AOrchestra/aorchestra/{runtime,prompts}`; sandbox
server `src/claw_eval/sandbox/server.py` was NOT opened — see R5 (the one item to
verify before treating B2 as fully scoped).*
