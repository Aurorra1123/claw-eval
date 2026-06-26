# Harness A/B Rollout — OpenClaw vs AOrchestra(pi) — 5-Task Sonnet 对比

**日期:** 2026-06-26
**模型:** `claude-sonnet-4-5`(via deepwisdom newapi,`https://newapi.deepwisdom.ai/v1`)
**并发:** `--parallel 5`,ProcessPoolExecutor,judge enabled
**Task 集(两路一致):** T002 / T008 / T012 / T018 / T077 — 跨 communication / productivity / finance / operations / office_qa 五类,easy×4 + hard×1,全部确定性 mock service(无外网、无 sandbox 工具)

来源:
- `docs/rollout_ao_pi_5task.md`(commit `0ef791e`)
- `docs/rollout_openclaw_5task.md`(commit `84594ef`)
- 并发就绪调研:`docs/rollout_aorchestra_concurrency.md`、`docs/rollout_openclaw_concurrency.md`

---

## 1. 总分对比

| | OpenClaw | AOrchestra(pi) |
|---|---|---|
| **avg score** | **0.793** | 0.708 |
| **pass rate** | 4/5 | 4/5 |
| **errored** | 0/5 | 0/5 |
| **real wall (5-task batch)** | ~120s | ~96s |
| **agent cost** | ~$0.48 | ~$0.086 |
| **执行形态** | 每 task 一 docker 容器 | host(无 docker) |

OpenClaw 总分略高(+0.085),但**成本高 5.6×、wall 慢 25%**。成本差的主因是 OpenClaw 无 prompt caching + 容器内 OpenClaw 自带的 system prompt/工具开销;AO-pi 的 pi 子进程更精简。

---

## 2. 逐 task 对比

| Task | 类别 | 难度 | OpenClaw | AO-pi | 谁赢 | 备注 |
|---|---|---|---|---|---|---|
| T002 email_triage | communication | easy | 0.78 | 0.805 | ≈平(AO+0.025) | 都 PASS |
| T008 todo_management | productivity | easy | 0.91 | 0.860 | OpenClaw +0.05 | 都 PASS |
| T012 expense_report | finance | easy | **0.46 FAIL** | **0.00 FAIL** | OpenClaw 损伤更小 | 见下 |
| T018 ticket_triage | operations | easy | 0.87 | **0.978** | AO +0.108 | 都 PASS |
| T077 officeqa (OCR) | office_qa | hard | **0.95** | 0.899 | OpenClaw +0.051 | 都 PASS;OpenClaw 命中 0.95 已知基线 |

**pass profile 完全一致**:两路都只在 T012 翻车,其余 4 个都过。

---

## 3. 唯一的失败:T012 — 两种失败模式

T012 要求 agent 检测并合并一对完全重复的交易(txn_002 + txn_003)后再 submit。两路都没做对 dedup,但**失败方式不同**,这是本轮最有信息量的发现:

| | 行为 | completion | safety | score |
|---|---|---|---|---|
| **AO-pi** | 提交了全部 13 笔交易(含重复对),grader 安全门检测到重复全在 → 全 0 | 0.0 | — | **0.00** |
| **OpenClaw** | 停下来反问"现在 submit 吗?"没完成提交 → 没提交错误报告 | 0.33 | 1.0 | **0.46** |

- **AO-pi 是"自信地做错"**:MainAgent 把子任务委托为"取数→求和→提交",从未把 dedup 需求传递给 sub-agent,sub-agent 提交了错误内容。AO 内部 `total_reward:1.0`(它自己的"sub-agent 完成了"信号)跟 claw-eval rubric 无关。
- **OpenClaw 是"谨慎地不做"**:sonnet 在 OpenClaw 循环里选择了反问澄清而非贸然提交,所以 safety 满分、没提交错误报告,拿到部分分。

**这正是 harness 设计差异的体现**:AO 的 MainAgent→SubAgent 委托链会"压平"任务要求(dedup 这种约束在委托时丢了);OpenClaw 的单 agent 循环保留了完整上下文,更倾向澄清。

---

## 4. 并发隔离 — 两路都证明干净

| | 端口分配 | 隔离证据 |
|---|---|---|
| **AO-pi** | port_offset 500 + slot*50:gmail 9600 / todo 9652 / finance 9704 / helpdesk 9757 / ocr 9821 | 零碰撞、零 EADDRINUSE、跨 task 答案零串台 |
| **OpenClaw** | sandbox_port 8080+slot*50:8080/8130/8180/8230/8280,全 host network | 5 容器唯一命名 + 唯一端口,实时抓取确认同时运行,自动清理 |

**反并发污染铁证(并发调研阶段)**:T077 trace 只含自己答案 36080(2 次)、T078 答案 031969(0 次),反之亦然。

---

## 5. 成本与 scale 估算

| | per-task wall | per-task cost | 5-task 真实 wall | 推外 50-task 估算(parallel 5) |
|---|---|---|---|---|
| AO-pi | ~19s | ~$0.017 | ~96s | ~16min / ~$0.86 |
| OpenClaw | ~31s | ~$0.096 | ~120s | ~20min / ~$4.8 |

**scale 瓶颈不是并发**(AO 端口上限 ~463 worker,OpenClaw ~16 worker)——是 **API rate limit + per-task 延迟 + (OpenClaw)docker/内存**。

---

## 6. 调试障碍(两路都撞到,记录供 scale 参考)

1. **无多 task-ID 选择 flag** —— `batch` 只有 `--filter`(单子串)/`--tag`/`--range`(连续),选 5 个非连续 ID 要复制/symlink task dir。两路都用了变通。**scale 到任意大 task 集前建议加 `--task-ids` flag**(见 memory `claweval-batch-task-selection-gap`)。
2. **symlink task dir 破坏 mock-service CWD** —— `_resolve_tasks_dir` 用 `parent.parent` 不 `.resolve()`,symlink 让 CWD 指到 /tmp。解法:把 task dir 放进 repo root 下的真实 sibling 目录,或加 `.resolve()`。
3. **officeqa grader 在 `--no-judge` 下崩** —— `judge.evaluate()` 无 None 守卫。**rollout 必须带 judge**。
4. **OpenClaw 特有**:venv 缺 `docker` Python SDK(`uv pip install 'docker>=7.0'`);docker bind-mount 要绝对路径(`--trace-dir` 传绝对路径)。

---

## 7. 结论

- **两个 harness 在这 5 task 上都可靠并发、零基础设施故障。** 并发修复(OpenClaw batch fix `1652869`)+ 进程隔离架构经实测验证。
- **OpenClaw 总分略高但贵 5.6× 慢 25%**;AO-pi 更省更快,在结构化任务(T018 ticket triage)上反超。
- **最有价值的发现是 T012 的失败模式分歧**:AO 的委托链压平任务约束(自信做错),OpenClaw 单 agent 保留上下文(谨慎不做)。这是 harness 架构差异的直接证据,值得在更大 task 集上验证是否系统性。
- **下一步 scale 建议**:先加 `--task-ids` flag 消除 task 选择障碍,然后挑一批"需要多步约束/委托"的 task(像 T012 这种)专门验证委托链压平假设。

### 待办 / follow-up

- [ ] `cmd_batch` 加 `--task-ids T002,T008,...` flag(或 `_resolve_tasks_dir` 加 `.resolve()`)
- [ ] officeqa grader 加 `if judge is None` 守卫(消除"必须带 judge"约束)
- [ ] pi runtime 的 token reporting gap(cost 只能靠 AO `total_cost`,claw-eval 的 token 效率维度拿不到)
- [ ] scale 到 ~20-50 task,重点覆盖多步委托类 task 验证 T012 类失败模式是否系统性

### 已完成 / DONE (2026-06-26 scale-readiness fixes)

三个 scale 障碍已全部修复(各自独立 commit + 测试 + 全量回归)。详见
`docs/rollout_scale_fixes.md`。基线 94 passed → 修复后 105 passed / 4 skipped(+11 tests)。

- [x] **`batch --task-ids` 多 ID 选择 flag** — `bd2a7fa`
  接受全名(`T002_email_triage`)或短号(`T002`),与 `--filter/--tag/--range`
  互斥(组合即报错),缺失 ID 列出报错而非静默跳过。**消除 copy/symlink task dir 变通。**
- [x] **`_resolve_tasks_dir` 加 `.resolve()`** — `ee5704a`
  symlink task dir 现在解析到真实位置,mock-service CWD 落在 repo root;非 symlink 路径行为不变。
- [x] **grader `--no-judge` 不再崩** — `5cadc43`(NoJudge 空对象,框架级单一 chokepoint)
  + `dd33427`(email-triage 直连 `judge.client` 的 30 次重试 hang 修复,集成检查时发现)。
  judge 维度记 0.0(安全,非静默通过),规则维度照常计分。

**集成验证(三修复同时跑通):** 1 task aorchestra host 模式(react runtime)、
`--task-ids T002_email_triage --no-judge`,无 copy/symlink,`batch_results.json`
正常写出(`error: None`,score 0.48),`--no-judge` grading 不崩不 hang。

**下一轮 scale 推荐命令(workaround 已消除):**

```bash
python -m claw_eval.cli batch \
  --tasks-dir tasks \
  --task-ids T002_email_triage,T008_todo_management,T012_expense_report,T018_ticket_triage,T077_officeqa_highest_dept_spending \
  --harness aorchestra --config config_concurrency_smoke.yaml \
  --parallel 5 --port-base-offset 500 --trials 1 --trace-dir traces/<run_name>
```

剩余未做:pi runtime token reporting gap;scale 到 20-50 task 验证 T012 类委托链压平假设。
