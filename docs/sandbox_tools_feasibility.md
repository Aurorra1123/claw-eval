# Sandbox tools feasibility & baseline-comparison fairness

**Date:** 2026-06-29
**Scope:** code + data analysis only (no re-running, no grading, no code changes).

**Motivation.** A Claude-Sonnet-4.5 *baseline* run outscored our recent
OpenClaw / AOrchestra (AO) 50-task rollouts on several `office_qa` tasks. Part
of the cause: the baseline agent was given **SANDBOX_TOOLS**
(`Bash/Read/Write/Edit/Glob/Grep/BrowserScreenshot/ReadMedia/Download`) and used
`Read`+`Bash`+`grep`+`sed` to precisely extract a table (T082 baseline = 0.92),
whereas our rollouts gave the agent only a domain tool such as
`ocr_extract_text` (whole document as one blob; T082 ours = 0.29–0.80).

This doc answers two questions:

1. **Can each of the three harnesses ENABLE sandbox tools, end to end?**
2. **How many of the 50 tasks are AFFECTED by the mismatch, and how big is the
   resulting score gap?**

`SANDBOX_TOOLS` are defined in
`src/claw_eval/runner/sandbox_tools.py:305-317` (the 9 tools above);
`SANDBOX_TOOL_NAMES` is the frozenset of their names.

---

## Q1 — Can each harness enable sandbox tools? (per-harness wiring)

### Two distinct CLI flags

`src/claw_eval/cli.py` exposes **two separate flags** on `run` and `batch`
(and `--sandbox-tools` only on the internal `_run-inner`). `grade` has neither.

| Flag | Subcommands | Meaning | cli.py |
|---|---|---|---|
| `--sandbox` | `run`, `batch` | Start a Docker **container** that runs the in-container sandbox HTTP server; sandbox tool calls route over HTTP to it. | `cli.py:1854`, `cli.py:1920` |
| `--sandbox-tools` | `run`, `batch`, `_run-inner` | Append the SANDBOX_TOOLS **toolset** and dispatch it **locally on the host** via subprocess/filesystem (no Docker). | `cli.py:1856`, `cli.py:1871`, `cli.py:1922` |
| `--sandbox-image` | `run`, `batch` | Override the container image (default `claw-eval-agent:latest`, `config.py:69`). | `cli.py:1855`, `cli.py:1921` |

How the flags reach the harness:

- **Sandbox (container) mode** — `cli.py:449-507` (single) / `cli.py:1015-1069`
  (batch worker): when `--sandbox`, the CLI starts a container, injects fixture
  files, and calls `harness.run(..., sandbox_handle=handle, sandbox_tools=True)`.
  Note `sandbox_tools=True` is **hardcoded** here (`cli.py:506`, `cli.py:1055`).
- **Local mode** — `cli.py:601-630` / `cli.py:1070-1081`: calls
  `harness.run(..., sandbox_handle=None, sandbox_tools=getattr(args,"sandbox_tools",False))`.
- A harness receives the truth through **two channels**: `sandbox_handle`
  (non-None ⇒ container/HTTP dispatch) and the `sandbox_tools` bool (host
  subprocess dispatch). For claweval, `claweval.py:84` ORs them:
  `use_sandbox_tools = sandbox_handle is not None or sandbox_tools`.

There are two **gates** in `cli.py`:

- **OpenClaw** is refused in host mode entirely (`cli.py:416-426`): `--harness
  openclaw` requires `--sandbox` (unless the `CLAWEVAL_ALLOW_OPENCLAW_HOST_SMOKE=1`
  test escape hatch is set).
- **AOrchestra** is refused in host mode **only when the task declares sandbox
  tools** (`cli.py:434-447`, mirrored in the batch worker `cli.py:975-990`):
  `--harness aorchestra requires --sandbox when the task declares sandbox tools
  [...]`. This is the gate that errored T068zh (`['Bash']`).

---

### (a/b) claweval (native loop) — **CAN: YES**

`runner/loop.py:run_task(..., sandbox_tools=False, sandbox_url=None)`:

- When `sandbox_tools=True`, it **appends the full SANDBOX_TOOLS set** to the
  task's tools (deduped against tools the task already declares):
  `loop.py:260-268`
  ```python
  if sandbox_tools:
      existing_names = {t.name for t in task.tools}
      sandbox_tool_list = [t for t in SANDBOX_TOOLS if t.name not in existing_names]
      task_tools = list(task.tools) + sandbox_tool_list
      dispatcher = SandboxToolDispatcher(http_dispatcher, sandbox_url=sandbox_url, ...)
  ```
  These tool specs are also added to the system prompt (`loop.py:319`,
  `extra_tools=sandbox_tool_list`).
- Dispatch (`runner/sandbox_dispatcher.py`):
  - `sandbox_url` **set** ⇒ HTTP to the container sandbox server
    (`_dispatch_remote`, `sandbox_dispatcher.py:114-115,162-188`); `_PATH_MAP`
    maps `Bash→/exec`, `Read→/read`, `Write→/write`, `Edit→/edit`,
    `Glob→/glob`, `Grep→/grep`, … (`sandbox_dispatcher.py:120-130`).
  - `sandbox_url` **None** ⇒ local subprocess / host filesystem
    (`_dispatch_local`, `sandbox_dispatcher.py:116,262+`).

So claweval has **two equivalent ways** to enable sandbox tools:

- `--sandbox` (container; `sandbox_handle` → `sandbox_url` set → HTTP dispatch into
  the container where fixtures were injected). This is the **baseline-matching**
  setup.
- `--sandbox-tools` (no Docker; appends the toolset and dispatches **on the
  host** via subprocess). Convenient but the agent then touches the **host**
  filesystem, not an isolated container.

**Verdict — claweval: YES.** Appends all 9 tools automatically. Container path
needs `--sandbox` (+ image); host path needs `--sandbox-tools`. This is the
harness the baseline traces were produced with.

---

### (c) OpenClaw — **CAN: PARTIAL** (only the bridge tools a task declares; never OpenClaw's own builtins)

OpenClaw runs the **OpenClaw CLI as a subprocess** (`openclaw agent --local
--json`, `_openclaw_native.py`; container via `docker exec`,
`_openclaw_container.py`). It does **not** call `run_task` and does **not** use
claweval's SANDBOX_TOOLS append path.

Key facts:

- OpenClaw's **own builtin file/shell tools are explicitly DENIED**.
  `_BUILTIN_TOOLS_TO_DENY` (`harnesses/openclaw.py`, ~`:72-111`) includes
  `read`, `write`, `edit`, `exec`, `browser`, `pdf`, `document_extract`,
  `web_search`, `web_fetch`, … The deny list is written into the OpenClaw config
  (`tools.deny`) for both the host-smoke and container paths. Purpose: the
  agent's *only* visible toolset during a task must be the **bridge** tools, so
  it can't read `tasks/<id>/grader.py` or escape the mock-service sandbox.
- Sandbox tools ARE reachable, but **only via the bridge plugin and only for
  tools the task DECLARES**. The bridge (`harnesses/_openclaw_bridge/`) inspects
  `task.tools`; any name in `SANDBOX_TOOL_NAMES` is bridged to the container
  sandbox server (`Bash→/exec`, `Read→/read`, …) and any name in
  `task.tool_endpoints` is bridged to the mock HTTP service. It does **not**
  append the full 9-tool set the way claweval's loop does.
- Container mode is required for sandbox tools (host smoke has no sandbox server
  to bridge to; preflight rejects SANDBOX tools on host).

So to make OpenClaw replicate the baseline office_qa behaviour you would have to:
1. **Edit the task.yaml** to declare `Read`/`Bash`/`Grep` in its `tools:` list
   (today office_qa declares only `ocr_extract_text`), and
2. run with **`--sandbox`** (container + bridge).

You would *not* un-deny OpenClaw's builtins — those operate on the container's
real filesystem outside the sandbox-server boundary (would expose grader files,
and would name-collide with / shadow the bridge `Read`/`Write` tools).

**Verdict — OpenClaw: PARTIAL.** Sandbox tools work **only** through the bridge
and **only** for tools the task explicitly declares, in `--sandbox` container
mode. Matching the baseline requires editing each office_qa task.yaml to declare
the sandbox tools; it cannot be turned on by a flag alone.

---

### (d) AOrchestra — **CAN: NO (today)** — container path is an unimplemented stub

The registry uses `harnesses/aorchestra/harness.py::AOrchestraHarness`
(`harnesses/aorchestra/__init__.py` re-exports it; note `_runner.py` is the
in-process MainAgent runner, **not** the dispatcher).

- `AOrchestraHarness.run` dispatches on `sandbox_handle`
  (`harness.py:110-125`): `sandbox_handle is not None` → `_run_container(...)`;
  else `_run_host_smoke(...)`.
- **`_run_container` is a stub that raises** (`harness.py:231-245`):
  ```python
  def _run_container(self, ...):
      raise NotImplementedError(
          "Wave 4-E will implement the container path "
          "(SANDBOX_TOOL_NAMES bridge to the sandbox server)."
      )
  ```
- Host smoke **actively refuses** sandbox tools as a second-line gate
  (`harness.py:148-157`): if `task.tools` contains any SANDBOX_TOOL_NAME it
  prints an error and `raise SystemExit(2)`.
- Even the (future) bridge would expose **only task-declared** sandbox tools, not
  the full set: `_bridge/env.py::_build_actions` iterates `self._task.tools`
  (`env.py:215`) and builds a `make_sandbox_action(...)` only when
  `tool.name in SANDBOX_TOOL_NAMES` (`env.py:216-218`); `make_sandbox_action`
  requires a non-None `sandbox_url` or raises (`_bridge/actions.py`). It never
  calls `get_sandbox_tools()` / appends `SANDBOX_TOOLS`.

So today: `--harness aorchestra` **without** `--sandbox` on a task that declares
sandbox tools → CLI gate error (`cli.py:434-447`); **with** `--sandbox` → the
harness hits the `NotImplementedError` stub. Tasks with **no** declared sandbox
tools run fine in host mode (this is how the AO 50-task rollout ran).

**Verdict — AOrchestra: NO (today).** The container / sandbox-tools path is not
implemented (Wave 4-E). When it lands it will, like OpenClaw, expose only
**task-declared** sandbox tools (+ `--sandbox` + `sandbox_url`), never the full
appended set.

---

### Q1 summary table

| Harness | CAN enable sandbox tools? | What it needs | Full set or task-declared? |
|---|---|---|---|
| **claweval** (native) | **YES** | `--sandbox` (container, baseline-matching) **or** `--sandbox-tools` (host subprocess) | **Full 9** appended automatically (`loop.py:260-268`) |
| **OpenClaw** | **PARTIAL** | `--sandbox` (container) **AND** edit each task.yaml to declare the sandbox tools so the bridge exposes them | **Only task-declared** (bridge inspects `task.tools`); builtins stay denied |
| **AOrchestra** | **NO (today)** | container path unimplemented (`harness.py:242` `NotImplementedError`, Wave 4-E). Would later need `--sandbox` + task-declared tools | **Only task-declared** (`_bridge/env.py:215-218`) — once implemented |

---

## Q2 — How many of the 50 tasks are AFFECTED?

**Definition.** A task is **AFFECTED** if the baseline trace used ≥1 of the 9
SANDBOX_TOOLS **and** our current `tasks/<id>/task.yaml` does **not** declare any
of the specific sandbox tools the baseline used.

**Method (no whole-file reads).** Baseline tool usage was extracted by streaming
the `"tool_name"` field out of each baseline trace
(`/tmp/tracepkg/final_clean_trace_package/traces/NNN_<id>_<hash>.jsonl`) with
`grep -ao '"tool_name":[ ]*"[^"]*"'` (line-delimited JSON; never loaded whole).
Trace structure verified on a small trace (T002): tool calls are
`tool_dispatch` events with a `tool_name` field. Task tools were parsed from each
`task.yaml` `tools:` block. Scores: baseline = `task_list.csv` col
`source_task_score`; AO = `traces/rollout_ao_50task/.../batch_results.json`
`avg_score`; OpenClaw = `/tmp/openclaw_gemini_regrade.tsv` `gemini_task_score`.

**Sanity check (passed):** T082 baseline tool counts = **`Bash`×4, `Read`×3**
(matches the known Python parse); our T082 task.yaml = `ocr_extract_text` only →
**AFFECTED**. ✔

### Counts

- **AFFECTED: 19 / 50**
- **NOT affected: 31 / 50**
- All 50 traces found and parsed.

The 19 split into two qualitatively different patterns:

- **Document-extraction pattern (8):** baseline used `Read`/`Bash`/`Grep` to
  read & parse a file. This is the documented advantage. The 4 pure `office_qa`
  tasks (T080, T082, T083, T084) are the core of it; also T074, T098, T007zh,
  T057.
- **Write-only pattern (11):** baseline used only `Write` (occasionally to dump
  an intermediate/output file). Whether this helped scoring is task-dependent and
  generally marginal — our harnesses submit answers through mock-service tools.

### AFFECTED list (baseline sandbox tools | our task.yaml | scores)

(office_qa first, then the rest; "ao=ERR" = AO task errored in the rollout.)

| task_id | baseline sandbox tools | our task.yaml tools | baseline | AO | OpenClaw |
|---|---|---|---|---|---|
| T080_officeqa_bond_yield_change | Bash,Read | 1 mock tool (no sandbox) | 0.35 | 0.28 | 0.36 |
| T082_officeqa_qoq_esf_change | Bash,Read | 1 mock tool (no sandbox) | **0.92** | 0.80 | **0.29** |
| T083_officeqa_mad_excise_tax | Bash,Read | 1 mock tool (no sandbox) | 0.40 | 0.95 | 0.91 |
| T084_officeqa_geometric_mean_silver | Bash,Grep,Read | 1 mock tool (no sandbox) | **0.89** | 0.79 | **0.28** |
| T007zh_todo_management | Bash | 4 mock tools (no sandbox) | 0.94 | 0.72 | 0.94 |
| T043zh_service_outage_research | Write | 3 mock tools (no sandbox) | 0.93 | 0.99 | 0.14 |
| T057_deepseek_logo_identification | Read | 3 mock tools (no sandbox) | 0.20 | 0.20 | 0.20 |
| T068zh_llama_w8a8_cuda_bug | Write | Bash,web_search,web_fetch | 0.46 | ERR | 0.56 |
| T074_paper_review_injection | Bash,Read | 1 mock tool (no sandbox) | 0.73 | 0.97 | 0.94 |
| T098_pinbench_openclaw_facts | Bash,Read | 1 mock tool (no sandbox) | 0.20 | 0.20 | 0.20 |
| T108_ticket_routing | Write | 8 mock tools (no sandbox) | 0.73 | 0.80 | 0.83 |
| T131zh_order_profit_analysis | Write | 6 mock tools (no sandbox) | 0.92 | 0.76 | 0.94 |
| T132_order_profit_analysis | Write | 6 mock tools (no sandbox) | 0.94 | 1.00 | 0.94 |
| T144_quarterly_business_insight | Write | 7 mock tools (no sandbox) | 0.66 | 0.72 | 0.97 |
| T149zh_project_progress_report | Write | 8 mock tools (no sandbox) | 0.53 | 0.93 | 0.93 |
| T151zh_supply_chain_investigation | Write | 10 mock tools (no sandbox) | 0.78 | 0.96 | 1.00 |
| T153zh_market_research_report | Write | 12 mock tools (no sandbox) | 0.75 | 0.97 | 0.73 |
| T154_market_research_report | Write | 12 mock tools (no sandbox) | 0.71 | 0.99 | 0.52 |
| T158_month_end_reconciliation | Write | 11 mock tools (no sandbox) | 0.90 | 0.66 | 0.65 |

Note: T068zh declares `Bash` in its task.yaml but the baseline used `Write`
(which it does not declare) → AFFECTED on `Write`. (It is also the task the AO
sandbox gate errored on.)

### Score-gap quantification (upper bound of the mismatch effect)

Gap = baseline − ours (positive ⇒ baseline higher; this is the *most* the
sandbox mismatch could explain).

| Subset | n | avg baseline | avg gap (base − AO) | avg gap (base − OpenClaw) |
|---|---|---|---|---|
| **All affected** | 19 | 0.68 | **−0.067** (AO higher) | **+0.032** |
| Extraction pattern (Read/Bash/Grep) | 8 | 0.58 | −0.033 | +0.065 |
| Write-only | 11 | 0.75 | −0.095 (AO higher) | +0.008 |
| **office_qa only (T080/82/83/84)** | 4 | 0.64 | **−0.062** (AO higher) | **+0.183** |

**Interpretation — the mismatch is real but narrow, and not a systematic baseline
advantage:**

- Across all 19 affected tasks, **AO already beats baseline on average**
  (−0.067) and **OpenClaw is within +0.03** — so the sandbox-tools gap does
  **not** explain a broad baseline edge.
- The only place the baseline's sandbox advantage clearly shows is **OpenClaw on
  specific office_qa tasks**: **+0.183** average, driven almost entirely by
  **T082 (0.92 vs 0.29)** and **T084 (0.89 vs 0.28)** — exactly the
  precise-table-extraction cases where baseline used `Read`+`Bash`. On T083 our
  harnesses actually *beat* baseline (OpenClaw 0.91, AO 0.95 vs 0.40), and on
  T080 everyone is low (~0.3).
- **AO shows no office_qa deficit** vs baseline on average; its office_qa scores
  came from the (in-process, no-sandbox) MainAgent reading the OCR blob and in
  some cases doing better than baseline.

---

## Recommendation — how to make a FAIR baseline comparison

The baseline was produced with **claweval + sandbox tools** (the native loop
appending all 9 tools). To compare apples-to-apples:

### 1. claweval — fully feasible, recommended (the true apples-to-apples baseline)

Re-run claweval **with the same sandbox setup the baseline used**. Two options:

- **Container (matches baseline exactly):**
  ```
  claw-eval batch --harness claweval --task-ids <ids> \
    --sandbox --sandbox-image claw-eval-agent:latest \
    --model <our-model> --base-url <our-endpoint> --parallel N
  ```
  `--sandbox` ⇒ `sandbox_tools=True` is forced (`cli.py:506/1055`), all 9 tools
  appended (`loop.py:260-268`), dispatched into the container where fixtures are
  injected.
- **Host (no Docker), quicker:** swap `--sandbox` for `--sandbox-tools` (host
  subprocess dispatch). Use only if you trust the host FS isolation.

Run this on **at minimum the 4 office_qa tasks** (T080, T082, T083, T084), ideally
the full extraction subset (add T074, T098, T007zh, T057). This isolates how much
of *our model's* gap vs the *baseline model* is the model itself rather than the
toolset. This is the comparison that can actually be made fairly today.

### 2. OpenClaw — feasible only with task.yaml edits (out of scope here)

OpenClaw can reach sandbox tools, but **only the ones a task declares**, via the
bridge in `--sandbox` mode. To make its office_qa scores comparable to baseline
you must **add `Read`/`Bash`/`Grep` to each office_qa task.yaml `tools:` list**
(they currently declare only `ocr_extract_text`) and re-run with `--sandbox`.
That is a task-definition change (not allowed under this analysis's
constraints) and would also change the task contract for *every* harness, so it
should be a deliberate, separate decision. **Without that edit, OpenClaw's
office_qa scores cannot be made comparable to the baseline's sandbox setup.**

### 3. AOrchestra — NOT feasible today

AO's sandbox/container path is an unimplemented `NotImplementedError` stub
(`harness.py:242`, Wave 4-E). **AO cannot be given the baseline's sandbox setup
at all right now** — host mode refuses sandbox tools, and `--sandbox` hits the
stub. So **AO's office_qa scores cannot be made comparable to the baseline**
until Wave 4-E lands; even then it would expose only task-declared tools, so it
would *also* require the task.yaml edits from option 2.

### Bottom line

- **The only harness that can be re-run to reproduce the baseline's sandbox
  setup as-is is claweval** (`--sandbox` or `--sandbox-tools`). Do that on the
  office_qa / extraction subset to get a fair model-vs-model comparison.
- The sandbox-tools mismatch explains a **narrow** effect: ~**+0.18** for
  **OpenClaw on office_qa**, concentrated in **T082 and T084**. It does **not**
  explain a broad baseline advantage — across all 19 affected tasks AO already
  matches/beats baseline and OpenClaw is within +0.03.
- For OpenClaw/AO to be compared on office_qa, the tasks would need their
  `tools:` lists extended to declare sandbox tools (OpenClaw) and Wave 4-E
  implemented (AO). Until then, **AO office_qa cannot be made baseline-comparable;
  OpenClaw office_qa cannot without task edits.**

---

*Analysis artifacts: per-task extraction in the analysis script (streamed
`tool_name` fields; not committed). Evidence cited inline as `file:line`.*
