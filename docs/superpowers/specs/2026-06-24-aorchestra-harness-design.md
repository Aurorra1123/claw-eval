# AOrchestra Harness 接入设计

**Status:** approved(brainstorming 完成,Section 1-6 全部用户批准,等 spec self-review + 用户最终 review)
**Date:** 2026-06-24
**Author:** Aurorra1123 + Claude
**Context:** Phase 4 — 把 AOrchestra(`/data2/ruanjianhao/AOrchestra/`,arxiv 2602.03786)接入 claw-eval 作为 first-class harness,跟 Wave 3 交付的 OpenClawHarness 平级。

---

## 目标 & 成功标准

- **目标**:`claw-eval --harness aorchestra` 能跑通 claw-eval task,产出跟 OpenClaw / ClawEval 同 schema 的 trace,被现有 grader 直接消费。
- **成功标准**:跟 OpenClaw 同条件(同 `claude-sonnet-4-5` 模型、同 task、同 mock service)对比,AOrchestra 路径 task_score 跟 OpenClaw 路径**差在 ±0.1 内**;长期希望 AOrchestra 路径 > OpenClaw 路径(验证 paper 的 16.28% 提升能在 claw-eval 任务上复现)。

---

## 关键决策(来自 brainstorming 9 个澄清问题)

| # | 维度 | 决策 | 替代选项 | 选这个的理由 |
|---|---|---|---|---|
| 1 | 整体目的 | first-class harness + 验证 score > OpenClaw | 只算分 / 只测 orchestration 价值 | 长期可复用 + 跨 harness 对比有意义 |
| 2 | 模型统一 | MainAgent + 所有 SubAgent 都用 `claude-sonnet-4-5` | 主大模 + 子小模 / 跑两套 | 严格同模型对比,后续真泡测再分大小模 |
| 3 | 工具集分发 | MainAgent 见 `claweval tools + DelegateTaskTool + CompleteTask`;SubAgent 见 `claweval tools` | 仅 SubAgent / 仅 MainAgent | 完整 AOrchestra 形态,公平接收同 task 输入 |
| 4 | 容器化范围 | AOrchestra 进程留 host,SANDBOX_TOOL_NAMES 桥到容器内 sandbox server | 进程也容器化 / 拒含 Bash 的 task | AOrchestra 是 Python in-process 库,跟 OpenClaw 独立 Node 进程不同——不存在"越界读 host"问题 |
| 5 | trace 翻译 | 扁平化 + `agent_role: "main"/"sub"/"agent"` 字段(default `"agent"`) | 完全扁平不标 / 嵌套保留 | 跟现有 trace schema 兼容 + 不丢 orchestration 层次信息 |
| 6 | e2e 验收 | OpenClaw 7 项里适用的 + 改 task_score 口径为 vs OpenClaw + 软记录 delegate 计数 | 硬要求 MainAgent delegate | LLM 行为不该是契约级要求,跟 Wave 3-E T068 同款处理 |
| 7 | 依赖管理 | `pip install claw-eval[aorchestra]` 可选 extras | 主 requirements / conda env | 跟 OpenClaw 的 `Dockerfile.openclaw` 可选性对称——不强加 144 个 deps 给所有用户 |
| 8 | 目录布局 | OpenClaw 不动,AOrchestra 进 `harnesses/aorchestra/` 子目录隔离 | 全部子目录化(重构 OpenClaw)/ 全部平铺 + 命名前缀 | 不动已 push 到 GitHub 的 OpenClaw 代码,AOrchestra 用更清晰的隔离布局,后续 codex/claudecode 真接时照 AOrchestra 走 |
| 9 | trace 摘要模型(_summarize_trace) | LLMsConfig 别名 `gemini-3-flash-preview` → `claude-sonnet-4-5`,不改 AOrchestra 代码 | 改 AOrchestra 源码 / 拒此机制 | AOrchestra 全代码只 1 处硬编码 model key,通过 config 别名最干净 |

---

## Section 1:架构总览

### 角色拓扑

```
[host]
├── ServiceManager (mock service: ocr/web_real/gmail/...)     ← 跟 OpenClaw 同款,host 上
├── Python 主进程 (claw-eval cli + AOrchestraHarness)
│   ├── import AOrchestra(它就是个 Python 库)
│   ├── ClawEvalEnv (新建 BaseEnv 子类,跟 GAIAEnv/SWE-BenchEnv 同级)
│   ├── ClawEvalAction × N (BaseAction 子类,HTTP 工具的 Python 封装)
│   └── MainAgent → SubAgent 编排,跑在主进程里
└── docker container (per-task,复用 Dockerfile.openclaw 镜像)
    ├── sandbox server                              ← 服务 Bash/Read/Write 等
    └── (没有 AOrchestra,因为 AOrchestra 留在 host)
```

### 跟 OpenClaw 拓扑的对比

| | OpenClaw | AOrchestra |
|---|---|---|
| 主进程在哪 | 容器内 | host |
| 容器里跑什么 | OpenClaw subprocess + sandbox server | 仅 sandbox server |
| 工具桥接 | bridge plugin(TS)+ npm install + docker exec | Python `BaseAction` 子类,直接 `httpx.post` |
| trace 抓取 | session.jsonl + bridge log 两源合并 + callID 匹配 | 我们自己控制 `env._step_log`,callID 自己生成,100% 匹配 |
| 镜像 | `claw-eval-agent-openclaw:latest` | **同款,复用**(只用到容器里的 sandbox server) |

### 关键化简(相对 OpenClaw)

1. **工具桥接从 ~600 行 TS plugin 编译 + npm install 链 → ~150 行 Python `BaseAction` 子类**
2. **trace 翻译从两源合并(session + bridge log)+ 三级匹配兜底 → 单源(直接读 env step log),无匹配问题**
3. **容器化只用容器内的 sandbox server,host 不动**——不需要 docker exec OpenClaw、不需要 `_openclaw_container.py` 等价物

---

## Section 2:模块布局与文件清单

```
src/claw_eval/harnesses/
├── base.py / __init__.py(改)/ claweval.py / openclaw.py / codex.py / claudecode.py   ← 不动 Wave 1-3
├── _openclaw_* / _trace_adapter.py / _snapshot.py                                       ← 不动
└── aorchestra/                                              ← 新增子目录
    ├── __init__.py
    ├── harness.py                                            ← AOrchestraHarness 主类
    ├── _runner.py                                            ← 配置 + 跑 AOrchestra Runner
    ├── _trace_adapter.py                                     ← trajectory.json + step_log → JSONL
    └── _bridge/
        ├── __init__.py
        ├── actions.py                                        ← ClawEvalAction 工厂(BaseAction 子类)
        ├── env.py                                            ← ClawEvalEnv adapter
        └── model_config.py                                   ← LLMsConfig 改写器

tests/
├── fixtures/aorchestra/{trajectory_sample.json, step_log_sample.jsonl}
├── test_aorchestra_bridge.py / test_aorchestra_trace_adapter.py
├── test_aorchestra_e2e.py(T077 host smoke)/ test_aorchestra_e2e_container.py(T068 容器 + Bash 桥接)

src/claw_eval/models/trace.py    ← 改:TraceMessage / ToolDispatch 加 agent_role: Literal["main","sub","agent"] = "agent"
src/claw_eval/cli.py             ← 改:--harness choices 加 aorchestra,按需起容器(见 §4)
pyproject.toml                   ← 改:[project.optional-dependencies].aorchestra
```

依赖管理 :`pip install claw-eval[aorchestra]` extras + `pip install -e /path/to/AOrchestra`(两步)。

---

## Section 3:数据流(prompt 进入 → trace 落地)

**关键时序点** ⚠ 标。

```
CLI: --harness aorchestra --task T077 --sandbox(若 task 含 SANDBOX_TOOLS,§4 强制)
  │
  ▼
ServiceManager.__enter__()                          ← mock service 起在 host
  │
  ▼
SandboxRunner.start_container(network_mode=host)    ← 仅当 task 含 SANDBOX_TOOLS 才起(见 §4 决策表)
                                                      复用 Dockerfile.openclaw,只用容器里的 sandbox server
  │
  ▼
AOrchestraHarness.run(task, sandbox_handle=...)
  │
  ├── 1. preflight 检查(§4 完整规则)
  │
  ├── 2. _bridge.model_config.inject_llms_config(cfg.model)
  │     - LLMsConfig._default_config 临时设为:
  │         {
  │           "claude-sonnet-4-5":      { base_url, key, ... },
  │           "gemini-3-flash-preview": { base_url, key, model:"claude-sonnet-4-5" },  ← 别名指向同 endpoint
  │         }
  │     - delegate.py:266 的 _summarize_trace 硬编码 gemini-3-flash-preview,
  │       通过别名实际拿到 claude-sonnet-4-5,**不改 AOrchestra 代码**
  │     - 注入与还原的契约见 §4.4(实现细节由 coding agent 自决)
  │
  ├── 3. with _bridge.env.ClawEvalEnv(task, sandbox_url, services_ctx) as env:
  │     - reset() 返回 task.prompt.text 作为初始 observation
  │     - get_action_space() 返回:
  │         - HTTP mock service 工具 (make_http_action 编译,目标 = task.tool_endpoints[*].url)
  │         - SANDBOX_TOOL_NAMES 工具 (make_sandbox_action,目标 = sandbox_url + endpoint)
  │     - 内部 _step_log: list[dict] 初始化空,context manager 退出时 close
  │
  ├── 4. _runner.create_main_agent(env, cfg) → MainAgent
  │     - main_model = "claude-sonnet-4-5"
  │     - sub_models = ["claude-sonnet-4-5"]              ← list 单元素
  │     - MainAgent 工具集 = env.get_action_space() + [DelegateTaskTool, CompleteTask]
  │     - SubAgent 工具集  = env.get_action_space()        + [CompleteTask]
  │       （SubAgent 不要 DelegateTaskTool,避免无限委派嵌套）
  │
  ├── 5. await _runner.run_one_task(main_agent, task)
  │     ┌───────────────────────────────────────────────────────────────────────┐
  │     │ MainAgent.step() loop:                                                │
  │     │   - 调 LLM → 得 action                                                 │
  │     │   - action == delegate_task:                                          │
  │     │       spawn SubAgent → SubAgent.step() loop:                          │
  │     │         - claweval_tool → ClawEvalAction.__call__()                   │
  │     │           → httpx.post(target_url) + env._step_log.append({           │
  │     │               toolCallId, agent_role:"sub", tool, url, request,       │
  │     │               status, response, durationMs                            │
  │     │             })                                                        │
  │     │         - complete → SubAgent return                                  │
  │     │     SubAgent trace 经 _summarize_trace 压缩(实际跑 claude-sonnet-4-5,│
  │     │     因为 step 2 的别名映射)                                            │
  │     │   - action == claweval_tool (MainAgent 自己调,Q3 允许):                │
  │     │       同上,agent_role:"main"                                          │
  │     │   - action == complete:                                                │
  │     │       MainAgent return,trajectory 写盘到 case_dir/raw/                │
  │     └───────────────────────────────────────────────────────────────────────┘
  │
  ├── 6. ⚠ services_ctx.collect_audit() / 容器 stop 前抓
  │
  ├── 7. SandboxRunner.inject_grader_files(handle, task)
  │     - ⚠ MainAgent return 之后,确保 agent 看不到 verify 答案
  │     - 仅当 sandbox_handle is not None 时才跑(若 task 不含 SANDBOX_TOOLS,sandbox_grader_files 通过 host workdir 注入)
  │
  ├── 8. env_snapshot(详见 §4 三种路径)
  │
  ├── 9. _trace_adapter.translate_aorchestra(...)
  │     - 输入:trajectory_path + step_log_path + audit_data + task metadata
  │     - 输出:claw-eval JSONL trace
  │
  ├── 10. ⚠ restore LLMsConfig 到注入前(契约见 §4.4)
  │
  └── 11. return HarnessResult(trace_path, env_snapshot, audit_data, raw_dir)

CLI: ServiceManager.__exit__()  → mock service 关闭
CLI: SandboxRunner.stop_container() (若起了的话)
```

### Trace 翻译表

| 来源 | 翻译为 |
|---|---|
| 开头一次 | `TraceStart(harness="aorchestra")` |
| 开头一次 | `TraceMessage(role=user, content=[TextBlock(task.prompt.text)], agent_role="main")` |
| trajectory MainAgent text step | `TraceMessage(role=assistant, content=[TextBlock], usage=TokenUsage, agent_role="main")` |
| trajectory MainAgent tool step | 上一条 assistant 追加 `ToolUseBlock(id=toolCallId, name, input)`;新 `TraceMessage(role=user, content=[ToolResultBlock], agent_role="main")` |
| trajectory SubAgent step | 同上,`agent_role="sub"` |
| `step_log` 每条 record | `ToolDispatch(tool_use_id=toolCallId, ...,  agent_role=record.agent_role)` |
| `audit_data` 每个 service | `AuditSnapshot(...)` 同 OpenClaw |
| 结尾一次 | `TraceEnd(harness="aorchestra", ...)` |

**关键差异 vs OpenClaw 翻译表**:
- 无 callID 匹配兜底——`toolCallId` 由 `ClawEvalAction.__call__` 内部 `uuid4().hex` 生成,同时塞进 step_log 和返回给 AOrchestra runtime,Level 1 100% 命中
- 多 `agent_role` 字段填充

### 数据流的关键不变式

| 不变式 | 保证机制 |
|---|---|
| `step_log[i].toolCallId` ↔ `trajectory[*].action.toolCallId` 一对一 | `ClawEvalAction.__call__` 内部生成 uuid + 双向塞 |
| audit_data 抓取时 mock service 还活着 | `ServiceManager` 在 `with` 块内一直活 |
| grader-only 文件 agent 看不到 | inject_grader_files 在 step 7,MainAgent return 之后 |
| LLM 调用全部 claude-sonnet-4-5 | step 2 LLMsConfig 注入 + 别名映射 |

---

## Section 4:错误处理与边界

### 4.1 preflight 决策表

`AOrchestraHarness.preflight(task)` 的完整规则:

| 条件 | 处置 | 理由 |
|---|---|---|
| `task.user_agent.enabled == True` | **拒**:`"aorchestra harness does not support simulated user_agent"` | AOrchestra 是 one-shot 跑 task,没有"模拟用户回话"机制 |
| `task.tools` 含 SANDBOX_TOOL_NAMES 且 CLI 没传 `--sandbox` | **拒**:`"task requires SANDBOX_TOOLS — please pass --sandbox"` | host 上跑 Bash 会越界(claw-eval 主进程权限) |
| `task.tools` schema 含 oneOf / allOf / $ref | **拒** | 跟 OpenClaw bridge 同款 preflight 拦截 |
| 否则 | **过**:`[]` | |

`task.services` / `task.tool_endpoints` 非空**不是**拒绝条件——这是 first-class harness 标志。

### 4.2 容器起停决策

```python
# CLI 逻辑
if args.harness == "aorchestra":
    task_needs_sandbox = any(t.name in SANDBOX_TOOL_NAMES for t in task.tools)
    if task_needs_sandbox and not args.sandbox:
        raise SystemExit(2)   # preflight 会拒,这里是双保险
    sandbox_handle = (
        SandboxRunner.start_container(image="claw-eval-agent-openclaw:latest",
                                       network_mode="host")
        if task_needs_sandbox else None
    )
```

**安全门跟 OpenClaw 不对称**:
- OpenClaw:**所有** task 都强制 `--sandbox`(进程本身要隔离)
- AOrchestra:**仅当 task 需要 SANDBOX_TOOLS** 才强制 `--sandbox`(Python in-process 不需要隔离)

CLI `--help` 文档要写清这两个 harness 安全门不同。

### 4.3 env_snapshot 路径(三种)

| 场景 | 路径 |
|---|---|
| task 含 SANDBOX_TOOLS(容器路径) | `_snapshot.from_sandbox_url(sandbox_url, task)` — **复用 OpenClaw 同款代码** |
| task 不含 SANDBOX_TOOLS 但有 `env_snapshot_*` | `_snapshot.from_workdir(work_dir, task, task_dir)` — **复用** |
| task 没有 snapshot 声明 | `env_snapshot=None`,grader 不读 |

Wave 3 已交付的 `inject_grader_files_host` / `collect_workdir_snapshot` / `from_sandbox_url` 全部复用,AOrchestra 不重写。

### 4.4 LLMsConfig 注入与还原(契约表达,不规定实现)

**契约**:`LLMsConfig._default_config` 是 AOrchestra 进程内全局单例。`AOrchestraHarness._run_*` 入口必须保证:

1. task 跑前注入 patched config(`claude-sonnet-4-5` 主 key + `gemini-3-flash-preview` 别名都映射到 claw-eval 的 endpoint)
2. task 跑完(或抛错)后**必须还原**为注入前的值
3. 还原在异常路径下也要成立——即使 `_runner.run_one_task` 抛了 exception,后续测试看到的 `LLMsConfig` 必须是干净的

实现细节(context manager / try-finally / 其他)由 coding agent 选择,**只要满足上述契约**。spec 不规定。

### 4.5 失败模式 — 复用 OpenClaw 同款 schema

OpenClaw 已实现的 `status` / `failure_modes` 设计(`_openclaw_native.py:1167-1187`、`_trace_adapter.py:439-446`)直接搬:

| AOrchestra 异常 | status | failure_modes |
|---|---|---|
| AOrchestra crash 没产 trajectory | `"error"` | `["error"]` 或捕获的 exception text |
| timeout(超过 `task.environment.timeout_seconds`) | `"timeout"` | `["timeout"]` |
| trajectory 部分写出,SubAgent crash | `"error"` | `[<per-step error message>, ...]` |
| 正常 | `"ok"` | `[]` |
| `import aorchestra` ImportError | 不到 trace 这一层——直接抛 `ImportError("pip install claw-eval[aorchestra] && pip install -e /path/to/AOrchestra")` |
| ClawEvalAction.__call__ httpx error | 不抛,记 `step_log({status: -1, error})`,返回错误响应让 LLM 自决重试(镜像 OpenClaw bridge `recordCall`) |
| MainAgent 没 delegate | 不 fail,trace_adapter 检查 agent_role 集合,只有 `"main"` 时在 raw_dir 写 warning(Q6 软记录) |

`_trace_adapter.translate_aorchestra` 要支持 partial 输入——trajectory 文件缺失或空就生成最小 trace(`TraceStart + TraceMessage(user prompt) + TraceEnd(failure_modes)`),让 grader 仍能算分(通常会得 0,但跟"完全没 trace"语义不同)。

### 4.6 trace 翻译降级

step_log ↔ trajectory 理论 100% 命中(都是我们自己控制),实测可能的异常:

| 异常 | 处置 |
|---|---|
| trajectory 有 toolCall,step_log 无对应 record | `ToolDispatch(response_status=500, response_body={"error":"no step_log record"})` + warning。理论 = ClawEvalAction bug |
| step_log 有 record,trajectory 无对应 toolCall | 生成 ToolDispatch 但不附 ToolUseBlock。理论也不应发生 |
| 双向都缺 | 不生成,grader robustness 维度自然忽略 |

---

## Section 5:测试策略

### 5.1 测试金字塔(跟 Wave 1-3 对称)

| 层级 | 文件 | 数量目标 | 触发 |
|---|---|---|---|
| 单元 — bridge actions | `tests/test_aorchestra_bridge.py` | ~15 | 每次 pytest 默认 |
| 单元 — trace adapter | `tests/test_aorchestra_trace_adapter.py` | ~8 | 默认 |
| 单元 — model_config patch | 合并进 bridge tests 或独立 | ~3 | 默认 |
| 单元 — placeholder/registry | 加入 `test_harness_placeholders.py` 参数化 | +1 | 默认 |
| e2e host smoke | `tests/test_aorchestra_e2e.py`(T077) | 1 | RUN_E2E=1 + LLM creds |
| e2e 容器版 | `tests/test_aorchestra_e2e_container.py`(T068) | 2 | RUN_E2E=1 + docker + image + LLM creds |

### 5.2 单元测试覆盖

**bridge actions / env / model_config**:

- `make_http_action`:`BaseAction` 子类 schema 对齐 task.tool / `await __call__` 真打 mock service / `step_log` append 正确字段 / httpx error 不抛而记 `status=-1`
- `make_sandbox_action`:url 形如 `{sandbox_url}/exec` / 缺 `sandbox_url` 抛 `SchemaTranslationError`
- `ClawEvalEnv`:reset 返回 prompt / get_action_space 跟 task.tools 一致 / `with` context manager close httpx
- LLMsConfig patch:patch 前后单例不变 / patch 期间 `get("gemini-3-flash-preview")` 返回 claw-eval endpoint / 异常路径 restore 不漏(用 `pytest.raises` 包测)

**trace adapter**:

- `test_translate_basic`:正常输入 → TraceMessage / ToolDispatch / TraceEnd 数量对
- `test_agent_role_filled`:MainAgent / SubAgent 事件 agent_role 分别 `"main"` / `"sub"`
- `test_load_trace_roundtrip`:`load_trace()` 完整反序列化(default `"agent"` 兼容老 trace)
- `test_grader_can_consume`:最简 mock grader 不抛
- `test_partial_trajectory`:trajectory 缺失 → 最小 trace + failure_modes 标记
- `test_step_log_callid_alignment`:trajectory ↔ step_log toolCallId 一一对应

**fixture 准备**(`tests/fixtures/aorchestra/`):
- `trajectory_sample.json` 手工构造,**遵循 AOrchestra 真实 schema**(参考 `aorchestra/runners/*.py`)
- `step_log_sample.jsonl` toolCallId 跟 trajectory 对齐
- **Follow-up**:e2e 跑通后,用真实 trajectory 把 fixture 更新一遍(降低手工构造跟实际 schema 漂移的风险)

### 5.3 e2e 测试

**`test_aorchestra_e2e.py`(host smoke, T077)** —— gate 同 OpenClaw:

- `@skipif(not RUN_E2E)` + `@skipif(not LLM creds env vars)`
- 不要 docker gate(host 模式)
- 验收 4 项:
  1. trace 文件存在 + `load_trace()` 通
  2. agent_role 字段填充(至少有 `"main"` 或 `"sub"`)
  3. task_score ≥ 0.3
  4. snapshot dict schema 正确
- **软记录**:e2e_report.json 含 `delegate_count`,不 fail
- 输出 `tmp_path / "e2e_report.json"` 同 OpenClaw 风格

**`test_aorchestra_e2e_container.py`(T068, Bash 桥接)** —— gate 加 docker + image:

- 7 项验收(跟 Wave 3-E 同形,有 AOrchestra 特化):
  1. callID 一致性(平凡过)
  2. step_log 完整性(每个 trajectory toolCall 有 step_log)
  3. **task_score 不验**——T068 无清晰 keyword grader,跟 OpenClaw Wave 3-E 同款处理;改用契约级"trace 能消费 + bridge 真桥到 sandbox /exec"
  4. snapshot OK
  5. **Bash 桥接验证**:step_log 中若出现 `tool="Bash"`,url 一定是 `<sandbox_url>/exec`(跟 OpenClaw 不同 — 那边验 plugin TS 源码,这边验 step_log)
  6. audit_data 入 trace
  7. agent_role 填充

### 5.4 跨 harness 对照(契约描述,实施在 §6 Wave 4-F)

- 选 5 个 task 同时跑 ClawEval / OpenClaw / AOrchestra
- assert `AOrchestra.task_score ≥ OpenClaw.task_score - 0.1`(同模型同 task,差 0.1 内算"打平或赢")
- 长期目标:AOrchestra 显著 > OpenClaw(paper 16.28% 相对提升的 claw-eval 复现)

---

## Section 6:落地节奏

跟 Phase 3 的 wave 模式对称,但因为 AOrchestra 比 OpenClaw 简单(Python in-process,无 plugin 编译,无 callID 匹配),wave 数和工作量都更少。

### Wave 4-A:门票验证(主对话直接做,~0.5 天)

**目标**:验证关键假设——`LLMsConfig` 单例注入真的能改 endpoint、`BaseAction` 子类真能被 SubAgent 调用、trajectory 真写到指定路径。

工作:
- `pip install -e /data2/ruanjianhao/AOrchestra` 进 claw-eval venv,无依赖冲突
- 写 ≤50 行 demo 脚本验证三件事
- 写 `docs/superpowers/specs/aorchestra_decision.md`,登记门票结论(类比 `docs/decision.md` 的 Phase 3 门票)

**出口判据**:三个验证都过 → 绿灯,进 4-B;任一不过 → 红/黄灯,回 brainstorming 改 spec

### Wave 4-B:bridge module(主对话直接做,~1 天)

工作:
- 新建 `harnesses/aorchestra/_bridge/`:
  - `actions.py`(make_http_action / make_sandbox_action)
  - `env.py`(ClawEvalEnv,with 块管理 httpx)
  - `model_config.py`(LLMsConfig patch + 别名映射)
- 写 `tests/test_aorchestra_bridge.py` ~15 单测,全过
- 不动 harness.py / _runner.py / _trace_adapter.py(后续 wave)

**出口判据**:15 单测全过 + 不影响 Wave 1-3 现有 61/61 测试

### Wave 4-C:trace adapter(派 subagent,可与 4-B 并行,~0.5 天)

输入:`trajectory_sample.json` + `step_log_sample.jsonl` fixture
输出:`harnesses/aorchestra/_trace_adapter.py` + 8 单测

**出口判据**:8 单测全过 + `load_trace()` 反序列化通过 + mock grader 能消费

### Wave 4-D:host smoke 集成 + T077 e2e(派 subagent,~1 天)

工作:
- 实现 `harnesses/aorchestra/harness.py` + `_runner.py`
- 注册到 `harnesses/__init__.py`
- `cli.py` 加 `--harness aorchestra` choices + 按需起容器逻辑(§4.2)
- `pyproject.toml` 加 `[aorchestra]` extras
- T077 host smoke e2e,4 项验收

**出口判据**:T077 task_score ≥ 0.3 + agent_role 字段填充 + soft delegate count 记录到 e2e_report.json

### Wave 4-E:容器 e2e + Bash 桥接(派 subagent,~1 天)

工作:
- **不打新镜像**,复用 `claw-eval-agent-openclaw:latest`(只用容器里的 sandbox server)
- T068 容器 e2e,7 项验收
- 重点验证:step_log 中 `tool="Bash"` 的 url 一定指向 `<sandbox_url>/exec`

**出口判据**:7 项核对全过(task_score 不强制,跟 Wave 3-E 同款处理)

### Wave 4-F:跨 harness 对照(用户手工,~1.5 小时)

不派 agent:
- 5-10 个 task 同时跑 ClawEval / OpenClaw / AOrchestra
- 写 `docs/cross_harness_phase4_report.md`
- 验证 `AOrchestra.task_score ≥ OpenClaw.task_score - 0.1`

### 总览

| Wave | 内容 | 形式 | 工时 |
|---|---|---|---|
| 4-A | 门票验证 | 主对话 | 0.5 天 |
| 4-B | bridge module | 主对话 | 1 天 |
| 4-C | trace adapter | subagent(并行 4-B)| 0.5 天 |
| 4-D | harness + T077 e2e | subagent | 1 天 |
| 4-E | T068 容器 e2e | subagent | 1 天 |
| 4-F | 跨 harness 对照 | 用户手工 | 1.5 小时 |
| **总** | | | **~4 天 + 1.5 小时** |

### 关键差异 vs Phase 3

| 维度 | Phase 3(OpenClaw) | Phase 4(AOrchestra) |
|---|---|---|
| wave 数 | 6 + 用户手工 | 5 + 用户手工 |
| 主对话直接做 | 仅 Wave 3-F | 4-A 门票 + 4-B bridge + 4-F 报告 |
| Dockerfile | 新建 Dockerfile.openclaw | **不新建,复用** |
| 关键风险 | callID 假设 / npm install / Bash 桥接 | 依赖冲突 / LLMsConfig restore / partial trajectory |
| 工作量 | ~9000 行 | 估 ~3000 行 |

---

## 决策溯源 / brainstorming session 摘录

- Brainstorming 跑了 9 个澄清回合(初始 8 个 + 增加的 "memory / summarizer 模型" 一回合)
- 探索 AOrchestra 代码用了 Explore agent + 主对话二次验证:
  - `base/agent/base_action.py` 是开放的 pydantic 接口 — 修正了 explore agent 报告的"工具构造时硬编码"为"无 plugin 机制但可直接传 BaseAction 实例"
  - `aorchestra/tools/delegate.py:266` 唯一一处硬编码 `gemini-3-flash-preview`,通过 `LLMsConfig` 别名映射统一指向 `claude-sonnet-4-5`,**不改 AOrchestra 代码**
- 依赖审计:AOrchestra `requirements.txt` 144 行,涉及 `litellm / datasets / daytona / e2b / modal / mini-swe-agent` 等大件 → 决策做成可选 extras
- 容器策略经过两次拍板才定稿:最初我推方案 X(一律起容器),用户问"按需 vs 一律",拍板 Y(按需) — 跟 OpenClaw 的"一律 sandbox"不对称,理由是 AOrchestra 是 Python in-process 库,跟 OpenClaw 独立 Node 进程不同
- LLMsConfig restore 措辞从"try/finally"改为契约表达(§4.4)——避免 spec 微管理 coding agent 的实现选择
