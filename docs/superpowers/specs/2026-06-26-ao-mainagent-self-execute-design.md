# 设计:AO MainAgent 自执行 + 委托传约束(claw-eval 专用 prompt)

**日期:** 2026-06-26
**作者:** 主对话(brainstorming skill)
**状态:** 待用户复核

---

## 1. 背景与动机

AO 的 `MainAgent` 在 claw-eval 这条路上**技术上能调业务工具**(`main_tools = [*main_claweval_tools, delegate_tool, complete_tool]`,`MainAgent.step` 是通用 dispatch:`tool = next(t for t in self.tools if t.name==action_name); await tool(**params)`),但**它借用的 `GAIAMainAgentPrompt` 把它定义成纯 orchestrator** —— DECISION PROCESS 只给两个出口:`complete` 或 `delegate_task`,从不提"你也可以自己调业务工具"。

实测后果(T012 expense_report,5-task rollout):
- MainAgent 把任务委托为一句压缩的 `task_instruction`("取数→求和→提交"),**原题的隐藏约束(检测并排除重复交易)在委托时丢失**。
- SubAgent 只看到压缩后的指令(原题仅 "for reference"),老实提交 13 笔含重复 → 触发 grader 的 `wrong_data` 安全门 → **0 分**。
- 对比 OpenClaw 单 agent(握有完整原题)谨慎反问拿 0.46。

**两个病根:**
1. **委托开销**:单步就能解决的简单任务(直接调一个 `finance_submit_report`),也被强制走"MainAgent 决策 → delegate → SubAgent 执行 → 回报 → complete"两层往返。
2. **约束压平**:MainAgent 委托时把任务压缩成一句话,隐藏约束/异常提示丢失。

---

## 2. 设计目标

让 MainAgent 从"纯调度器"变成"会干活的协调者":
- **简单任务自己执行**(直接调业务工具,省委托开销 + 保留完整上下文)
- **复杂/纯执行类任务委托 SubAgent**(分解、并行、不同模型分工)
- **委托时把原题的关键约束/异常提示写进 `context`**,不让 SubAgent 盲做

判断"简单 vs 复杂"**由 LLM 自己根据原则决定**(不写死阈值)。

---

## 3. 归属与影响面

GAIA 是另一个 benchmark;claw-eval 现在借 `GAIAMainAgentPrompt` 是权宜(`_runner.py` 注释自己写了 "because claw-eval tasks are single-shot Q&A in shape")。

**新增** `aorchestra/prompts/claweval.py`,内含 `ClawEvalMainAgentPrompt` 类:
- 与 `GAIAMainAgentPrompt` **接口一致**(同样的 `build_prompt(instruction, meta, prior_context, attempt_index, max_attempts, sub_models, subtask_history, model_to_alias, tools)` 静态方法签名),这样 `MainAgent` 的调用方式零改动。
- 复用 `format_tools_description` / `build_model_pricing_table` 等 `gaia.py` 里的 helper(import 过来,不重复)。

**接入点(一行)**:`claw-eval/src/claw_eval/harnesses/aorchestra/_runner.py` 构造 `MainAgent(... prompt_builder=GAIAMainAgentPrompt ...)` → 改成 `prompt_builder=ClawEvalMainAgentPrompt`。

**影响面**:只影响 claw-eval 跑 AO 这条路。GAIA / TerminalBench / SWE-bench 原生路径完全不碰。`SubAgent` prompt **不动**。

---

## 4. Prompt 设计(ClawEvalMainAgentPrompt)

在 `GAIAMainAgentPrompt` 的结构基础上,改 DECISION PROCESS 为**三出口**,并强化委托时的约束传递。核心改动:

### 4.1 三个动作 + 判断原则

```
You are the MainAgent. You can solve the task in THREE ways:

A. **Do it yourself** — call a business tool directly (the AVAILABLE TOOLS
   below are callable by you, not just delegatable). Prefer this when the
   task is simple: it can be finished in a few direct tool calls, needs no
   sub-task decomposition, and you can hold the full requirement in mind.

B. **Delegate** — use 'delegate_task' for work that is complex (multiple
   sub-steps, needs decomposition), purely executional (long mechanical
   sequences), or benefits from a dedicated sub-agent / different model.

C. **Complete** — use 'complete' once you have the answer (whether you did
   it yourself or a sub-agent did).

JUDGING SIMPLE vs COMPLEX (you decide):
- Simple → do it yourself: "submit this report", "look up X and answer",
  single-service lookups, anything ≤ a handful of tool calls.
- Complex → delegate: multi-service workflows, anything needing a plan,
  tasks where a focused sub-agent with a scoped instruction helps.
- When unsure and the task is short: prefer doing it yourself (avoids the
  delegation round-trip and keeps full context).
```

### 4.2 委托时强制传约束

```
WHEN YOU DELEGATE — preserve constraints:
The sub-agent only sees the task_instruction + context you write. The
original QUESTION often hides requirements (de-duplication, edge cases,
anomalies, "exclude X", precise totals). Before delegating:
1. Re-read the QUESTION for hidden/implicit requirements.
2. Write them EXPLICITLY into the `context` field — do not assume the
   sub-agent will re-derive them.

Example context: "The transaction list may contain exact duplicates;
detect them and EXCLUDE one before submitting. Verify the total excludes
the duplicate."
```

### 4.3 输出格式(三选一 JSON)

保留 `GAIAMainAgentPrompt` 的 JSON 输出形态,但加上"直接调业务工具"这一支:

```
If doing it yourself — call a business tool:
{ "action": "<business_tool_name>", "reasoning": "...", "params": {...} }

If delegating:
{ "action": "delegate_task", "reasoning": "...",
  "params": { "task_instruction": "...", "context": "<INCLUDE hidden
  constraints>", "model": "...", "tools": [...] } }

If done:
{ "action": "complete", "reasoning": "...", "params": { "answer": "..." } }
```

注:`MainAgent.step` 的 dispatch 已支持任意 `action_name`(查 `self.tools`),所以 `"action": "finance_submit_report"` 这种会被正确执行。**执行层零改动。**

### 4.4 保留的部分

`GAIAMainAgentPrompt` 里的 BUDGET AWARENESS、MODEL SELECTION GUIDE、Progress、QUESTION、SUBTASK HISTORY、AVAILABLE TOOLS 各节**保留**(claw-eval 也需要 budget/model 意识)。ANSWER FORMAT 节也保留(claw-eval 任务也要精确简短答案)。

---

## 5. 不做什么(YAGNI)

- **不改 SubAgent prompt**:约束丢失在委托层治,SubAgent 不动。
- **不加硬阈值判断**:简单/复杂由 LLM 判。
- **不动执行层 / dispatch 逻辑**:`MainAgent.step` 已通用,零改动。
- **不动 GAIA/TB/SWE prompt**:隔离。
- **不做改前改后 A/B**:按用户决定,只跑改后验证。

---

## 6. 验证方式(只跑改后)

改完后跑一批 claw-eval task(AO harness,含):
- **T012_expense_report**(隐藏 dedup 约束)→ 看是否从 0 脱离(MainAgent 自执行正确提交 OR 委托时把 dedup 传进 context 让 SubAgent 做对)。
- **几个单步简单 task**(T002 email / T008 todo / T018 ticket)→ 看 trace 里 MainAgent 是否出现**直接业务工具调用**(`action != delegate_task`),证明自执行路径激活。
- **一个多步复杂 task** → 确认仍然走 delegate(没把所有东西都自己干,退化成单 agent)。

判据:
1. T012 脱 0(或显著改善)。
2. 至少一个简单 task 的 trace 出现 MainAgent 直接业务工具调用。
3. 复杂 task 仍委托(架构没退化)。

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| LLM 过度自执行,复杂任务也自己干→退化成单 agent | prompt 明确"复杂/多步/需分解→delegate";验证 #3 检查复杂 task 仍委托 |
| LLM 还是不调业务工具(惯性 delegate) | 输出格式里明确给出"直接调业务工具"的 JSON 样例;AVAILABLE TOOLS 节强调"callable by you" |
| 委托传约束让 context 变长 → 成本上升 | 约束提示简短(一两句);只在真有隐藏约束时加 |
| 三出口让 MainAgent 决策更难、attempts 变多 | 验证时看 attempts 数;若暴涨则收紧"简单优先自执行"的引导 |

---

## 8. 交付物

- 新增 `aorchestra/prompts/claweval.py`(`ClawEvalMainAgentPrompt`)
- 修改 `aorchestra/prompts/__init__.py`(导出新类)
- 修改 `claw-eval/_runner.py` 一行(`prompt_builder=ClawEvalMainAgentPrompt`)
- 验证跑(几个 task,看上述三判据)

AOrchestra 侧改动走"decision-9 style"直接 patch(无上游 PR);claw-eval 侧一行改动正常入 git。
