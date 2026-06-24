# OpenClaw 工具注入门票验证 — 决策记录

本文件记录验证过程中的关键决策、依据、和被否定的备选方案。每条决策按时间顺序追加。

---

## 背景

Phase 3 设计中最难的点是 tool bridge:claw-eval 296/300 任务依赖 HTTP mock service,
OpenClaw 是 external CLI,默认看不到 claw-eval 的工具集。门票问题:**OpenClaw 能不能 per-run
接受外部注入的工具,并让工具实现路由到 claw-eval mock service?**

如果不能,Phase 3 只能缩到 4 个无 services 的任务;如果能,Phase 3 可以覆盖大部分任务。

---

## 决策 1:OpenClaw 工具注入有两条可行路径

**结论**:OpenClaw 2026.6.8 提供两条工具注入机制,**都可用**:

### 路径 A:MCP server(`openclaw mcp add`)
- stdio 命令或 HTTP URL
- 必须按 MCP 协议实现
- 每个 task 要起独立 MCP server,工作量重

### 路径 B:Tool plugin(`openclaw plugins init`)— **首选**
- Node module,`defineToolPlugin` 注册
- 工具参数用 TypeBox schema(`Type.Object({...})`)
- `execute: async (args) => {...}` 直接 `fetch()` 任意 HTTP endpoint
- 无需把 mock service 改造成 MCP
- 同一个 plugin 可以承载多个工具(`tools: (tool) => [tool({...}), tool({...})]`)

**为什么选路径 B**:
1. 工具实现是任意 JS,**直接 fetch claw-eval mock service** 即可,零改造。
2. 一个 plugin 文件能动态生成 N 个工具,从 `task.tool_endpoints` 编译,工程量可控。
3. plugin 注册后通过 `openclaw plugins enable/disable` 切换,适合 per-task 启停。

**风险**:
- plugin 是 Node module,绑死 OpenClaw 的 plugin-sdk 接口(`openclaw/plugin-sdk/tool-plugin`)。
  上游升级要重新校验。
- 工具描述呈现给模型的方式跟 claw-eval 的 OpenAI tool schema 风格可能不同,这是
  cross-harness 可比性的隐性损失项(已在 harness_design.md 5.3 节登记)。

**证据来源**:
- `openclaw plugins init demo` 生成的脚手架:`scratch/openclaw_tool_probe/demo_plugin/`
- 关键文件:
  - `openclaw.plugin.json` — plugin manifest
  - `src/index.ts` — `defineToolPlugin({id, name, tools: (tool) => [...]})`
  - `package.json` — `peerDependencies: {"openclaw": ">=2026.5.17"}`,
    `openclaw.extensions: ["./dist/index.js"]`

### 否决的路径

- **改 `models.json` 注入 tools**:OpenClaw 没有这种入口,工具集与模型分离。
- **走 `agents/<id>/agent/tools.json`**:不存在这个文件,工具来源是 plugins + MCP。
- **让 OpenClaw 用 `shell` curl mock service**:行为不可控,trace 里看到的是 shell 调用而非
  目标工具名,失去工具语义。

---

## 决策 2:实验任务选择

**选择**:`tasks/T077_officeqa_highest_dept_spending`

**理由**:
- 单工具任务:`ocr_extract_text`(input: `{image_path}`)
- 单 mock service:`ocr_t51`,端口 9121,POST `/ocr/extract`(FastAPI,
  `mock_services/ocr/server.py`)
- 无 `user_agent.enabled`(避免多轮模拟)
- 无 `sandbox_grader_files` / `env_snapshot_commands`(避免容器/快照复杂度)
- 评分清晰:keywords_present(`["36080","36,080"]`)+ tool_called + llm_judge
- 参考解仅 1 步:`ocr_extract_text(treasury_bulletin_1958_10.pdf)` 然后从返回的
  文本里找答案 36,080

**关键判据**(决定是否通过门票):
1. OpenClaw plugin 注册后,session.jsonl 出现 `toolCall: ocr_extract_text`
2. mock service 的 `/audit` 收到一次或多次 POST `/ocr/extract`
3. 模型在收到 OCR 文本后能继续推理并产生 `<FINAL_ANSWER>36080</FINAL_ANSWER>` 或近似

---

## 决策 3:门票验证结论(绿/黄/红)

**结论:🟢 绿灯。Phase 3 可以走 tool 桥接路线,但建议分阶段。**

### 已落实的确凿证据

1. **OpenClaw 2026.6.8 已安装**,版本与 `Workspace-Bench/evaluation/docker/Dockerfile`
   里 `npm install -g openclaw` 一致。
2. **`openclaw plugins init` 生成的脚手架本身就证明了 plugin 机制是稳定 API**——它由
   官方提供模板,在 `openclaw/plugin-sdk/tool-plugin` 模块下导出 `defineToolPlugin`。
3. **`defineToolPlugin` 接口允许任意 JS 实现工具**,关键证据来自
   `node_modules/openclaw/dist/plugin-sdk/tool-plugin.d.ts`:
   ```typescript
   ToolPluginToolDefinition = {
     name: string;
     description: string;
     parameters: TSchema;   // TypeBox -> JSON Schema
     execute: (params, config, context) => unknown;  // async 任意实现
   }
   ```
   `execute` 内部可以 `fetch()` 任意 URL,**包括 claw-eval 的 mock service**。
4. **mock service 是标准 FastAPI**(`mock_services/ocr/server.py`),起停顺利
   (验证过 `GET /ocr/health` 和 `POST /ocr/extract` 都返回预期数据)。

### Phase 3 由此变成可行的"工具桥接"

一个 task 跑前的流程是:

```
1. 起 task.services 里的 mock service (复用 ServiceManager)
2. 临时目录里动态生成一个 openclaw plugin:
   - plugin id = "claweval-bridge-<task_id>"
   - 从 task.tool_endpoints 里编译每个工具:
     defineToolPlugin({
       id, name,
       tools: (tool) => [
         tool({
           name: "ocr_extract_text",
           description: "...",                    // 抄 task.yaml
           parameters: Type.Object({              // 抄 input_schema
             image_path: Type.String(),
           }),
           execute: async (params) => {
             const r = await fetch(
               "http://localhost:9121/ocr/extract",
               { method: "POST", body: JSON.stringify(params) }
             );
             return await r.json();
           },
         }),
       ],
     })
3. openclaw plugins build + plugins enable
4. openclaw agent --local --json --message "<prompt>"
5. session.jsonl 抽 toolCall,GET /ocr/audit 抓服务端状态
6. plugins disable + 清理临时 plugin
```

### 但仍要警惕的事项(降级到🟡黄灯的条件)

1. **未实测 e2e**:本次验证停在"证据链充分,但没跑出来一次真实 LLM toolCall"。
   原因是再往下走要(a)起 gateway daemon、(b)注入模型 auth profile、(c)build plugin、
   (d)做 OpenClaw 完整 setup wizard。这些都是已知工程量,不是门票问题。
   **建议 Phase 3 第一周内做完一次 e2e,再正式开足马力。**
2. **工具描述呈现给模型的方式跟 claw-eval 的 OpenAI tool schema 不同**——这是
   cross-harness 可比性的隐性损失项(已在 harness_design.md §5.3 登记)。
3. **plugin SDK 是 Node 模块,绑死 `openclaw` 版本**(`peerDependencies:
   {"openclaw": ">=2026.5.17"}`)。OpenClaw 上游升级要重新校验。这是长期维护成本。
4. **execute 返回值的 schema 没有强约束**,直接透传 mock service 的 JSON——模型能不能
   消化要在 e2e 实测中观察。如果发现某些 mock service 返回结构模型无法理解,需要在
   bridge 里做一层适配(claw-eval mock service 一致性问题)。

### Phase 3 推荐节奏(基于今天的验证更新)

| 阶段 | 工作 | 预期 |
| --- | --- | --- |
| Week 1 | 手工写 1 个工具的 bridge plugin,跑通 T077 一次 e2e | 验证 toolCall 真的发生、audit 有真实记录、模型能消化 mock service 返回 |
| Week 2 | `task.yaml → plugin source` 生成器,跑 5–10 个任务对照 | 看 plugin schema 生成是否覆盖所有 task.yaml 写法 |
| Week 3 | ToolDispatch 翻译 + AuditSnapshot 抓取 + plugin 生命周期管理 | trace 跟 claweval 路径同构,可以直接进 grader |
| Week 4 | 全量跑 service 类任务对照实验 | 出 cross-harness 分数分布,定 Phase 3 收尾 |

### 否决的备选路径

- ❌ **方案 A:把 mock service 改造成 MCP server**——需要给每个 service(>=10 个不同
  实现)写 MCP 适配层,工作量 5x 于 plugin 方案。
- ❌ **方案 C:让 OpenClaw 用 shell curl mock service**——trace 里看到的是 shell 调用而
  非工具名,语义丢失,无法 cross-harness 比较 robustness 维度。
- ❌ **方案 D:Phase 3 直接缩范围到 4 个无 services 任务**——覆盖面 1.3%,不足以
  证明 cross-harness 评分体系。

### 副产物 / 残留状态

- `scratch/openclaw_tool_probe/demo_plugin/` — OpenClaw 官方脚手架生成的样本 plugin,
  保留作为 Week 1 复刻的参照。
- `scratch/openclaw_tool_probe/state/` & `home/` — 隔离 profile 的 OpenClaw 状态目录,
  保留可复用;Week 1 直接接着用。
- 已 kill 实验用的 OCR mock service 进程。
