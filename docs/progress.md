# Phase 3 开发进程

按 `docs/harness_design.md` §6 落地。**严格按 wave 推进,失败回滚到上一 wave**。

---

## 派发节奏(由依赖关系决定)

```
Wave 1 (1 agent, 顺序):  §6.1 Harness 骨架 + ClawEvalHarness
                              ↓ 阻塞
Wave 2 (3 agents, 并行): §6.2 _openclaw_native | §6.3 bridge | §6.4 翻译器
                              ↓ 三者都通才进下一波
Wave 3 (2 agents, 顺序): §6.5 OpenClawHarness e2e → §6.6 容器化 + §6.8 占位
                              ↓
Wave 4 (用户手工):       §6.7 全量对照实验(LLM 成本 + 时长由用户判定)
```

每 wave 完成后,主对话核对 diff、跑回归、更新本文档,再决定下一 wave。

---

## Wave 1 — Harness 骨架(零回归薄封装)

**对应 §6.1**

| 字段 | 值 |
| --- | --- |
| 状态 | ✅ 完成,5/5 回归通过,主对话验收 |
| Agent 类型 | general-purpose |
| Agent ID | a4cc93fb40bc34fcd |
| 派发时间 | 2026-06-24 |
| 完成时间 | 2026-06-24 |
| 核心交付 | `harnesses/{base,__init__,claweval}.py` + `cli.py` `--harness` 参数 + `TraceStart.harness` 字段 |
| 验收硬约束 | 同一 task `--harness claweval` 跑出来,trace 跟改造前**仅差** `TraceStart.harness` 一个字段 |
| 风险点 | 改 `cli.py` 时容易碰坏现有路径(sandbox / non-sandbox / batch / _run-inner) |

### 完成判据

- [x] `src/claw_eval/harnesses/__init__.py` 暴露 `get_harness(name)` registry
- [x] `src/claw_eval/harnesses/base.py` 含 `Harness Protocol / HarnessResult / HarnessFeature`(按 §3.2)
- [x] `src/claw_eval/harnesses/claweval.py` 含 `ClawEvalHarness`(按 §3.3)
- [x] `src/claw_eval/models/trace.py:TraceStart` 加 `harness: str = "claweval"`
- [x] `cli.py` 的 `run` / `_run-inner` / `batch` 三个子命令加 `--harness` 参数,默认 `claweval`
- [x] `cli.py` 的 `run_task(...)` 调用点改为 `get_harness(args.harness).run(...)`
- [x] 跑 5 个现有 task 回归测试,trace 差异**只有** `TraceStart.harness` 一个新字段
- [x] `tests/test_harness_claweval_regression.py` 存在,且通过

### Agent 自报的偏离(均经主对话审核接受)

- **4a env_snapshot 仍在 CLI 而非 harness 内**:为了在 docker 路径下保留
  `run_task → inject_grader_files → _collect_env_snapshot` 的次序,避免 grader-only
  文件泄露给 agent。`HarnessResult.env_snapshot` 接口保留,OpenClaw 这种自带 snapshot
  能力的 harness 可以填,CLI 优先使用 `result.env_snapshot`,为空则自己跑 fallback。
- **4b `sandbox_tools: bool = False` 新增 kwarg**:claw-eval 现有 `--sandbox` 和
  `--sandbox-tools` 是两个独立 flag,无法用 `sandbox_handle is None` 区分。additive
  kwarg 是最小侵入解,外部 harness 可忽略。

### 验收 Log

- 改动文件 7 个:5 新建(`harnesses/__init__.py / base.py / claweval.py`、`tests/conftest.py`、
  `tests/test_harness_claweval_regression.py`)+ 2 修改(`cli.py` 144+/77-、`models/trace.py` 4+)。
- 禁区零改动:`runner/loop.py`、`graders/`、`scoring.py` 全部 untouched。
- `grep -n "run_task\s*(" src/claw_eval/cli.py` 零残留 —— 5 个调用点全部迁移成 `harness.run(...)`。
- 回归测试 5/5 通过(`pytest -p no:quadrants tests/test_harness_claweval_regression.py`)。
  覆盖 task:`C01zh_mortgage_prepay` / `C02zh_personal_finance` / `C03en_real_estate_finance` /
  `C05zh_personal_finance_2` / `C10zh_labor_law`。
  测试逐 event 对比 trace JSONL,strip 的仅是 timestamp/trace_id/wall_clock 这些纯噪声字段,
  其他字节必须完全匹配 —— **真正的零回归 assertion**。

---

## Wave 2 — 三件并行(native runner / bridge / translator)

**对应 §6.2 / §6.3 / §6.4**

| 子任务 | 状态 | Agent ID |
| --- | --- | --- |
| §6.2 `_openclaw_native.py` 移植 + 单测 | ✅ 完成,4/4 通过,主对话验收 | a01682ca865789879 |
| §6.3 bridge plugin 生成器 + 单测 | ✅ 完成,29/29 通过,主对话验收 | a4055117d440145a1 |
| §6.4 trace 翻译器 + fixture 单测 | ✅ 完成,6/6 通过,主对话验收 | a8ff74a4194bf15d8 |

每个 agent 独立工作目录隔离:
- 2A 只动 `_openclaw_native.py` + 它的单测
- 2B 只动 `_openclaw_bridge/` + 它的单测
- 2C 只动 `_trace_adapter.py` + 它的单测 + `tests/fixtures/openclaw/`

三方不共享文件,无冲突风险。

### Wave 2A 验收(✅ 完成)

- 行数 1211(upstream 1199,+12),`diff` 显示**仅 2 处插入**:1 行 `extra_plugins` 参数 + 11 行注释/env 注入逻辑
- 其余 1198 行 verbatim 拷贝,业务逻辑无任何修改
- `extra_plugins` 通过 env var `CLAWEVAL_EXTRA_PLUGINS` 跨进程暴露,空值不导出,保证零行为变化
- 4/4 静态 smoke 测试通过,Wave 1 5/5 回归测试不回归(9/9 共通)
- 禁区全部 untouched

### Wave 2B 验收(✅ 完成)

- 文件:5 新源码(`__init__.py / generator.py / schema_translate.py / plugin_template/`)+ 1 测试
- **300 task 全量编译通过**:191 OK + 109 空集早退 + 0 错误。比 grep 数的 45 个多——`task.tool_endpoints: []` 也算空集
- T077 实际生成的 TS 跟设计文档 §3.4a 样本一致,真实 URL + recordCall 完整字段
- 测试 29/29 通过
- 两处自报偏离均接受:
  - `BridgeHandle` 字段顺序按 Python dataclass 语法约束调整(kwargs 构造无差)
  - `parse_traffic_log` 顺手加(Wave 2C trace adapter 要消费 jsonl,schema 放在生成方旁边合理)
- 禁区全部 untouched

### Wave 2C 验收(✅ 完成)

- 文件:1 新源码(`_trace_adapter.py`)+ 1 测试 + 2 fixture
- Fixture 透明性:`executionTrace_sample.json` agent 自报**hand-constructed**,理由——WorkspaceBench 真实 OpenClaw runs 全因 LLM context overflow 失败,没有 tool event 可抄。schema 已核对跟 `_openclaw_native._extract_openclaw_trace`(L820-939)真实产出一致。
- 测试 6/6 通过
- 5 处自报语义模糊点都合理,均接受
- **已知未实现项:Level 2 顺序匹配兜底**。Agent 严格按 §6.4 描述实现 Level 1 + Level 3,Level 2 是 §3.5 后加但 §6.4 未同步描述。**处置**:Wave 3 §6.5 e2e 实测 callID 假设;若假设成立(95% 预期成立),Level 2 永远用不上,不补;若不成立,回头加。
- 全部 44/44 测试通过(Wave 1 5 + 2A 4 + 2B 29 + 2C 6)
- 禁区全部 untouched

---

---

## Wave 3 — OpenClawHarness 集成 + 容器化 + 占位

**对应 §6.5 / §6.6 / §6.8**

| 子任务 | 状态 |
| --- | --- |
| §6.5 OpenClawHarness 集成 + T077 e2e + _snapshot.py | ✅ 完成,4/4 验收通过,task_score=0.9244,主对话验收(2026-06-24) |
| §6.6 容器化(Wave 3-E 主轴)| ✅ 完成,**主对话独立 e2e 验证**:T077 容器版 0.9524 + T068 Bash 桥接 OK,50/50 单测,2/2 e2e 通过(2026-06-24)|
| §6.8 codex/claudecode 占位类(Wave 3-F)| ✅ 完成,主对话直接交付,11/11 placeholder 单测通过(2026-06-24)|

### Wave 3-E 验收(✅ 完成)

**文件**:
- 4 新建:`Dockerfile.openclaw`(74 行,镜像 1.54GB)、`harnesses/_openclaw_container.py`(`docker exec` 驱动)、`tests/test_openclaw_e2e_container.py`(7 项验收 e2e)
- 5 修改:`harnesses/_openclaw_bridge/generator.py`(additive `sandbox_url` 参数)、`harnesses/_openclaw_bridge/__init__.py`(导出 SANDBOX_ENDPOINTS)、`harnesses/openclaw.py`(分流 container vs host_smoke)、`runner/sandbox_runner.py`(additive `network_mode/volumes/extra_env` kwargs)、`cli.py`(安全门:`--harness openclaw` 默认必须 `--sandbox`)

**禁区零改动**:`_openclaw_native.py`(Wave 2A verbatim,仍 1211 行)、`runner/loop.py`、`graders/`、`models/scoring.py` 全部 untouched

**关键决策(agent 选 + 主对话核可)**:**路 A docker exec**。理由——§3.7 要求 OpenClaw 进程关进容器,路 B(host 跑 OpenClaw + 容器只装 sandbox server)违背防作弊核心。Agent 写了 sibling 模块 `_openclaw_container.py` 实现 docker exec,复用 `_openclaw_native` 的轨迹提取 helpers,**不改 Wave 2A 移植代码**。代价:usage proxy + fetch hook 不复用(documented in module docstring)——session.jsonl 自带 llm.usage 字段足够。

**主对话独立 e2e 验证**:
- 跑了 `RUN_E2E=1 python -m pytest tests/test_openclaw_e2e_container.py`,330 秒(5.5 分钟)真实跑通
- T077 容器版:`task_score=0.9524`,callID 1 matched / 0 unmatched,bridge log 1==1,audit_in_trace=True,blocked_violations=[]
- T068 Bash 桥接:`bridge_has_bash_url=True`(`http://localhost:8080/exec`),`sandbox_url` 正确指向容器内 sandbox server
- 跟 Wave 3-D host 版 task_score 差:**+0.028**(0.9524 vs 0.9244,远小于 ±0.1 阈值,§6.5 第 3 项 ✅)

**7 项强制验收全过**(基于 T077 报告):
1. ✅ callID 一致性:1 matched
2. ✅ bridge log 完整性:1 session == 1 bridge
3. ✅ task_score 合理性:0.9524 跟 host 版差 < 0.1
4. ✅ snapshot schema:dict 返回,keys 为空(T077 无 env_snapshot_commands)
5. ✅ Bash 桥接(T068):bridge_has_bash_url=True
6. ✅ audit 时序:audit_in_trace=True
7. ✅ 内置工具屏蔽:blocked_violations=[]

**Agent 自报 5 处偏离,主对话审核结论**:
1. `SANDBOX_ENDPOINTS` 从 `SandboxToolDispatcher._PATH_MAP` 抄(原 prompt 写的 `_DEFAULT_ENDPOINTS` 不存在)—— ✅ 接受,functional equivalent,9 个 SANDBOX_TOOLS 都有 endpoint
2. `SandboxRunner.start_container` 加 3 个 additive kwarg(`network_mode/volumes/extra_env`)—— ✅ 接受,验证 additive(claweval 现有调用方默认不传)
3. T068 测试改成"bridge URL 路由验证",不强制要求模型调 Bash —— ✅ 接受,LLM 自主决定调不调,bridge 路由正确性是设计目标
4. CLI `--harness openclaw` 默认拒绝(必须带 `--sandbox`),escape hatch 通过 env var —— ✅ 接受,实测安全门生效("ERROR: --harness openclaw requires --sandbox for production")
5. audit_data 验证用 `result.audit_data is not None` 而非 inspect 单个 AuditSnapshot —— ✅ 接受,实际效果等价

**Wave 3-E agent 自报的 1.54GB 镜像**主对话独立确认:`docker images claw-eval-agent-openclaw:latest` 真实存在,size 1.54GB,OpenClaw 2026.6.10。

---

### Wave 3-D 验收(✅ 完成)

**重要 caveat:T077 跑得干净不能推广到全部 task**:
- T077 工具集是单一 `ocr_extract_text`,**不含任何 SANDBOX_TOOL_NAMES**(`Bash` /
  `Read` / `Write` 等)。所以 Wave 3-D host 实现刚好不需要碰到"工具桥到哪"的难题。
- 38 个声明 `Bash` 的 task 在 Wave 3-D host 模式下**跑不通**(bridge 不知道把 Bash
  桥到哪——host 上跑 Bash 会越界访问 task root)。这些 task 必须等 Wave 3-E 容器化
  + SANDBOX_TOOLS 桥接才能跑。
- `communication=0.0` 不是 bug——是 T077 grader 没实现这一维度(claweval 路径跑 T077
  也是 0)。task_score 计算 `0.8×0.9055 + 0.2×1.0 = 0.9244` 正确。

- **文件**:3 新建(`harnesses/openclaw.py` 459 行、`harnesses/_snapshot.py` 190 行、
  `tests/test_openclaw_e2e.py` 388 行)+ 2 修改(`harnesses/__init__.py` 加 openclaw、
  `cli.py` 3 个子命令 `--harness` choices 加 `openclaw`)
- **禁区零改动**:`runner/`、`graders/`、`models/scoring.py`、Wave 1/2 文件全部 untouched
- **真实 e2e 跑过**:`e2e_report.json` 在 `/tmp/pytest-of-root/pytest-56/.../e2e_report.json`,
  完整数据:
  - **callID 一致性 ✅**:1 matched / 0 unmatched / all_matched=True。Level 1 主匹配
    100% 覆盖,**Level 2 fallback 不需要补**(§3.5 假设成立)
  - **bridge log 完整性 ✅**:session 1 toolCall + bridge 1 record,严格相等
  - **task_score 合理性 ✅**:0.9244 远高于 0.3 阈值,completion=0.9055 / robustness=1.0
  - **snapshot OK ✅**:keys 空(T077 无 env_snapshot_commands)但 dict 返回正确
- **额外核对**:session.jsonl 里**只调用了 `ocr_extract_text`** 一个工具——OpenClaw
  自带 40+ 内置工具全部被 `tools.deny` 屏蔽,§6.5 第 4 项 "模型唯一可见工具集 == bridge
  plugin 暴露的工具" 契约 ✅
- **bridge_traffic.jsonl 内容真实**:`toolCallId / tool / url / status:200 / response` 都是真实 HTTP 调用记录
- **回归测试**:无 RUN_E2E 时 44/44 通过 + e2e 正确 skip;有 RUN_E2E 时 e2e 真跑(130 秒)
  并完整核对 4 项

### Wave 3-D agent 自报的工程发现(已记录,Wave 3-E 要承接)

1. **LLM base_url 必须带 `/v1` 后缀**:base_url 后面加 `/v1` 才能命中 chat completion
   endpoint。e2e 测试通过 `CLAWEVAL_LLM_BASE_URL` 环境变量配置(从硬编码改为 env var
   是为了不把 secret 推上 GitHub)。
2. **`tools.profile` 机制不可用**:OpenClaw 2026.6.x 的 `profile=minimal` + `tools.allow`
   会触发 "No callable tools remain" 错误。**实际可工作的方案是 `tools.deny` 枚举内置
   黑名单**(37 个内置工具)+ 保留 `session_status` 等 read-only 兜底。`_BUILTIN_TOOLS_TO_DENY`
   是经验性列表,**OpenClaw 升级时要重新校验**。
3. **`_openclaw_native` 隔离目录对齐**:agent 决定**不改 native runner 签名**,而是把
   bridge 的 `case_dir` 设为 `<sandbox_dir>/raw`,跟 native runner 自己建的隔离目录路径
   完全重合,两个进程读同一个 state_dir。

### Wave 3-D 验收结论与下一步

**Wave 3-D 完整跑通**,host smoke test 路径已经能产出合理分数,§3.5 / §3.6 / §6.5 的
所有假设在 T077 上**全部成立**。具体后果:

- Level 2 顺序匹配 fallback **不必补**——Level 1 假设成立
- bridge plugin 桥接路径**整体可行**
- 经验性 `_BUILTIN_TOOLS_TO_DENY` 在 Wave 3-E 容器化时需要被验证仍有效

**Wave 3-D 是 host smoke,Wave 3-E 才是生产形态**。文档已修订(§3.4 / §3.4a / §3.6 /
§3.7 / §5.4 / §6.5 / §6.6),Wave 3-E 的 prompt 需要重新写——它不再是"小工程量的可选优化",
而是承接 Wave 3-D 验证 + 加入 SANDBOX_TOOLS 桥接 + 真正防作弊隔离的主轴工作。

---

---

## Wave 4 — 用户手工触发

**对应 §6.7**

不派 agent。用户自行决定何时跑 30-50 个 task 的对照实验,产出 `docs/cross_harness_report.md`。

---

## 全局风险记录

- 文档锚点固定:任何 wave 出现"按方案做不下去"的情况,**优先回报、不要擅自调方案**。
  方案改动应由主对话决策,不由 subagent 做。
- `docs/` 在 `.gitignore` 里,代码改动正常入 git,文档改动留在本地。

---

## 变更日志

- 2026-06-24: 初始化 progress.md,派发 Wave 1。
- 2026-06-24: Wave 1 完成,主对话验收通过,准备派发 Wave 2 三个并行 agent。
- 2026-06-24: Wave 2 3 个 agent 并行派发(2A `_openclaw_native` / 2B `_openclaw_bridge` / 2C `_trace_adapter`)。
- 2026-06-24: Wave 2 全部完成,主对话逐个验收通过。全套 44/44 测试通过。准备派发 Wave 3。
- 2026-06-24: 根据用户提出的 3 项工程风险(env_snapshot 完整流程 / toolCallId fallback / safety task_score),修订 harness_design.md §3.5 §3.6 §5.2 §5.3 §6.5 §6.7。
- 2026-06-24: 派发 Wave 3-D(OpenClawHarness 集成 + T077 真实 e2e),Agent ID a5303202c7d2b2af6。
- 2026-06-24: Wave 3-D 完成,4/4 e2e 验收全过,task_score=0.9244。Level 1 callID 假设成立。
- 2026-06-24: 根据用户提出的容器化必要性 + SANDBOX_TOOLS 桥接讨论,修订 harness_design.md §3.4 §3.4a §3.6 §3.7(新增) §5.4 §6.5 §6.6。Wave 3-E 升级为主轴。
- 2026-06-24: 派发 Wave 3-E(Dockerfile.openclaw + bridge SANDBOX_TOOLS 桥接 + T077/T068 容器 e2e),Agent ID ab5d7a48aec3c6195。
- 2026-06-24: Wave 3-E 完成,主对话独立 e2e 验证通过(2/2 e2e 通过 330s,T077 容器版 task_score=0.9524 ≈ host 版 0.9244,差 +0.028 在容差内;T068 Bash 桥接 URL 验证 OK)。安全门生效。50/50 单测通过。
- 2026-06-24: Wave 3-F 完成,主对话直接交付(工作量小不派 agent):`codex.py` + `claudecode.py` 占位类,registry 4 槽位全开,CLI choices 全接受,preflight 立即拒绝 + NotImplementedError fail-loud。11 placeholder 单测全过,全套 61/61 通过。**Phase 3 全部交付完成**。
