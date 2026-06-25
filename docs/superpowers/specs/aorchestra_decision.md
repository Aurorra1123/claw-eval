# AOrchestra Harness 门票验证(Wave 4-A)

**Date:** 2026-06-24
**Status:** 🟢 GREEN — 进入 Wave 4-B
**Spec:** `docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md`
**Plan:** `docs/superpowers/plans/2026-06-24-aorchestra-harness.md` Task 1

---

## 验证目标

确认 spec §3 / §4.4 / §6 Wave 4-A 三个核心假设:

1. AOrchestra 能跟 claw-eval 共存(导入不报错、不冲突)
2. `LLMsConfig` 单例注入能改 endpoint
3. `BaseAction` 子类能被 AOrchestra runtime 当工具用

---

## 实测结果

### 1. 安装方式 — **plan 假设需更正**

**Plan 写的**:`pip install -e /data2/ruanjianhao/AOrchestra`

**实际**:**AOrchestra 没有 `setup.py` / `pyproject.toml`**,不是 pip-installable。
按 `AOrchestra/README.md`:它就是个项目仓库,用 `PYTHONPATH=/data2/ruanjianhao/AOrchestra` 或
`sys.path.insert(0, ...)` 注入即可。

**结论**:Wave 4-B / 4-D 的 harness 代码需要在 import AOrchestra 前注入 `sys.path`。
具体做法在 `aorchestra/harness.py` 顶层:

```python
import sys
_AORCHESTRA_ROOT = "/data2/ruanjianhao/AOrchestra"   # configurable via env var
if _AORCHESTRA_ROOT not in sys.path:
    sys.path.insert(0, _AORCHESTRA_ROOT)
```

或更通用 — 环境变量:

```python
import os, sys
_aorchestra_root = os.environ.get("AORCHESTRA_ROOT", "/data2/ruanjianhao/AOrchestra")
if _aorchestra_root not in sys.path:
    sys.path.insert(0, _aorchestra_root)
```

`pyproject.toml [aorchestra]` extras 仍然提供 AOrchestra 的 transitive 依赖
(`aiofiles / litellm / loguru / pyyaml / tiktoken`),只是不包含 AOrchestra 本身。

### 2. LLMsConfig 注入机制 — **plan 假设需更正**

**Plan / spec §4.4 写的**:`LLMsConfig._default_config = LLMsConfig({"model": ..., "key": ...})`

**实际**:`LLMsConfig.get(name)` 内部读 dict 的 **`"api_key"`** 字段(`async_llm.py:144`),
**不是** `"key"`。dict 里写 `"key"` 会被读成 `None`。

**结论**:Wave 4-B Task 5 的 `build_llms_config` dict schema 必须用 **`api_key`**,不是 `key`:

```python
inner = {
    "claude-sonnet-4-5": {
        "api_key": cfg_model.api_key,        # NOT "key"
        "base_url": cfg_model.base_url,
        "temperature": 0,
    },
}
```

`get()` 返回的 `LLMConfig` 对象本身的属性是 `.key`(`async_llm.py:20`),但这是
`get()` 从 dict 的 `api_key` 字段映射出来的。**dict 里写 `api_key`,LLMConfig 里读 `.key`**。

### 3. Gemini 别名映射 — **spec 决策 9 不可行,改方案**

**Spec §4.4 / §决策 9 写的**:`LLMsConfig` 里加一个 `gemini-3-flash-preview` 别名,
其 dict 的 `model` 字段指向 `claude-sonnet-4-5`,让 AOrchestra `delegate.py:266` 的
`_summarize_trace` 通过别名实际跑 sonnet。

**实际**:`LLMsConfig.get(name)` 在 `async_llm.py:142` **强制覆盖** `LLMConfig.model = name`:

```python
llm_config = {
    "model": llm_name,  # Use the key as the model name  ← 强制
    ...
}
```

所以无论 dict 里写什么 `model`,`get("gemini-3-flash-preview")` 返回的
`LLMConfig.model` 一定是 `"gemini-3-flash-preview"`,下游 `AsyncLLM` 直接用这字符串作
为 OpenAI API 的 `model` 参数发请求。

**实测确认** `newapi.deepwisdom.ai` 真的支持 `gemini-3-flash-preview` —— 跑出来是
Google Gemini("I am a large language model, trained by Google"),不是 claude。

**结论**:别名映射死路。**改 AO 源码**(决策 9 反转,获得用户批准):
`aorchestra/tools/delegate.py:266` 把硬编码的 `"gemini-3-flash-preview"` 改成
`self.models[0]`(自动跟 sub_models 走,我们都设成 `["claude-sonnet-4-5"]`,所以
summarizer 也跑 sonnet,跟 spec §决策 2 "全部统一同一模型" 一致)。

具体改动:

```python
# aorchestra/tools/delegate.py:264-271 (was)
try:
    review_llm = create_llm_instance(
        LLMsConfig.default().get("gemini-3-flash-preview")
    )
    return (await review_llm(prompt)).strip()

# (now)
try:
    # Phase 4 (claw-eval integration): use the first available sub-model
    # rather than a hardcoded gemini key. Keeps the harness contract of
    # "all LLM calls go through the configured model".
    review_llm = create_llm_instance(
        LLMsConfig.default().get(self.models[0])
    )
    return (await review_llm(prompt)).strip()
```

(L175 / L207 注释也同步更新,避免误导)

**Wave 4-B Task 5 后续影响**:`build_llms_config` 仍然只产 `"claude-sonnet-4-5"` 一个 key
(因为 summarizer 现在直接查 `self.models[0]`,不需要 gemini 别名)。**spec §4.4 第 1 条**
"gemini-3-flash-preview 别名映射"的说法**移除**;Task 5 plan 同步简化。

### 4. BaseAction 可被 AOrchestra runtime 用 — **plan 假设成立**

`BaseAction` 是开放的 pydantic 子类。`__call__(**kwargs) -> str` async 接口,
`to_param()` 返回 OpenAI tool format dict。Wave 4-B Task 3 直接用 plan 描述的方式工作。

### 5. 回归测试 — **plan 假设成立**

```bash
python -m pytest tests/ -p no:quadrants 2>&1 | tail -3
# 61 passed, 3 skipped, 3 warnings in 1.40s
```

Wave 1-3 所有 61 个测试零回归(claw-eval 主体测试不 import AOrchestra,所以
AOrchestra 的 144 个 transitive 依赖不影响 claw-eval 运行)。

---

## Plan 修订项(进 Wave 4-B 时必须照做)

| Plan 位置 | 原写法 | 修正 |
|---|---|---|
| Task 1 Step 1 | `pip install -e /data2/ruanjianhao/AOrchestra` | 用 `PYTHONPATH` 或 `sys.path.insert`,AOrchestra 不是 pip 包 |
| Task 5 build_llms_config | dict 用 `"key"` | 用 `"api_key"` |
| Task 5 build_llms_config | 加 `"gemini-3-flash-preview"` 别名 entry | **只**保留 `"claude-sonnet-4-5"` 一个 entry;summarizer 通过 `self.models[0]` 自动用同模型 |
| Task 5 tests | `test_build_llms_config_has_primary_and_alias` | 改成 `test_build_llms_config_has_primary_only`,删除 alias 相关 assertion |
| AOrchestra 仓库 | "不改 AO 源码" | **已改** `aorchestra/tools/delegate.py:266`(决策 9 反转,用户批准) |
| Task 7 harness.py | 入口加 `sys.path.insert(0, AORCHESTRA_ROOT)` 注入 | 必须做,否则 `from aorchestra import ...` 失败 |
| Task 7/8 e2e | 测试用 `pip install -e ...` | 测试入口前 `sys.path.insert(0, "/data2/ruanjianhao/AOrchestra")` 或 env var `AORCHESTRA_ROOT` |

---

## 残留风险(Wave 4-B 入口前要再次确认)

1. **AORCHESTRA_ROOT 路径**:hardcoded 还是 env var?
   - 推荐 env var with hardcoded default,因为 Aurorra1123 的环境固定在 `/data2/ruanjianhao/AOrchestra`,但
   要让 claw-eval CI / 其他用户能 override。
2. **依赖冲突未测**:claw-eval 之前没装 `litellm / daytona / e2b / modal / mini-swe-agent` 等大件。
   如果 user `pip install claw-eval[aorchestra]`,这些会装到 claw-eval venv 里,可能跟 claw-eval 已有依赖冲突。
   Wave 4-B / 4-D 真正用 AsyncLLM 调真实 LLM 时再测,这里只验证了"import 不报错"。
3. **AOrchestra 自带工具(GoogleSearchAction 等)是否会污染 SubAgent 工具集**:Wave 4-D harness.py 实施时需注意,
   传给 SubAgent 的 tools list 只能是 `env.get_action_space_for("sub") + [CompleteTask]`,**不**包含 AOrchestra 自带工具。

---

## 出口

🟢 **GREEN**:核心假设全部可验证,门票通过。3 处 plan 修订项已登记。

进入 **Wave 4-B(主对话直接做)**:Tasks 2-5,按修订后的方式实施。
