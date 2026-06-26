# AO MainAgent Self-Execute + Delegate-with-Full-Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give AO's MainAgent (on the claw-eval path) a claw-eval-specific prompt that lets it (a) execute simple tasks directly via business tools, (b) delegate complex/executional work while packing full task-relevant context into the sub-agent's `context`, (c) complete — fixing the T012-style failure where the delegation chain flattened a hidden dedup constraint to a 0.00 score.

**Architecture:** New `ClawEvalMainAgentPrompt` class in AOrchestra (interface-identical to `GAIAMainAgentPrompt`), wired in via a one-line switch in claw-eval's `_runner.py`. No execution-layer change (MainAgent.step already dispatches arbitrary `action_name` against `self.tools`). SubAgent prompt, GAIA/TB/SWE prompts untouched.

**Tech Stack:** Python 3.11+, AOrchestra at `/data2/ruanjianhao/AOrchestra/`, claw-eval at `/data2/ruanjianhao/claw-eval/`. Spec: `docs/superpowers/specs/2026-06-26-ao-mainagent-self-execute-design.md`.

## Global Constraints

- **Implementation site is split:** the new prompt class + export live in AOrchestra (`/data2/ruanjianhao/AOrchestra/`); the one-line wiring switch lives in claw-eval. AOrchestra changes use the "decision-9 style" direct patch (no upstream PR). claw-eval change is a normal commit.
- **Interface parity:** `ClawEvalMainAgentPrompt.build_prompt` MUST have the exact same signature as `GAIAMainAgentPrompt.build_prompt`: `build_prompt(instruction, meta, prior_context, attempt_index, max_attempts, sub_models, subtask_history="", model_to_alias=None, tools=None) -> str` (static method). MainAgent calls it by keyword (`aorchestra/main_agent.py:172`), so names must match exactly.
- **Reuse, don't duplicate:** import `format_tools_description` from `aorchestra.prompts.gaia` and `build_model_pricing_table` from `aorchestra.main_agent` (same as gaia.py does).
- **No execution-layer change.** `MainAgent.step` already does `tool = next(t for t in self.tools if t.name==action_name); await tool(**params)`. Business-tool self-execution works for free once the prompt invites it.
- **SubAgent prompt is NOT modified.** Constraint loss is fixed at the delegation layer (MainAgent writes full context), not in the SubAgent.
- **GAIA / TerminalBench / SWE-bench prompts are NOT modified.** Only claw-eval's MainAgent prompt changes.
- **Git identity** is NOT configured on AOrchestra — pass `GIT_AUTHOR_NAME="Aurorra1123" GIT_AUTHOR_EMAIL="Aurorra1123@users.noreply.github.com" GIT_COMMITTER_NAME="Aurorra1123" GIT_COMMITTER_EMAIL="Aurorra1123@users.noreply.github.com"` env vars on every AOrchestra commit. claw-eval git identity is configured.
- **pytest in this env** crashes with bus error unless `-p no:quadrants`, and crashes with `-v`. Use `python -m pytest <path> -p no:quadrants --tb=short`.
- **Sonnet creds for the validation run:** `CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1`, `CLAWEVAL_LLM_API_KEY=sk-u3HGm150NcbCJu2ohLB6BIgktG8V3QmVz8LA5JSTeDQXHhEo`, `CLAWEVAL_LLM_MODEL=claude-sonnet-4-5`, `CLAWEVAL_AORCHESTRA_RUNTIME=pi`, `AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra`, `no_proxy=localhost,127.0.0.1`.

---

## File Structure

### New (in AOrchestra)
- `aorchestra/prompts/claweval.py` — `ClawEvalMainAgentPrompt` class with the three-action prompt.

### Modified (in AOrchestra)
- `aorchestra/prompts/__init__.py` — export `ClawEvalMainAgentPrompt`.

### Modified (in claw-eval)
- `src/claw_eval/harnesses/aorchestra/_runner.py:431,439` — import + use `ClawEvalMainAgentPrompt` instead of `GAIAMainAgentPrompt`.

### New (in AOrchestra, tests)
- `tests/runtime/test_claweval_prompt.py` — unit tests for the prompt builder (renders, contains the three actions, contains the context-packing guidance, interface parity with GAIA).

---

## Task 1: `ClawEvalMainAgentPrompt` prompt class + unit tests

**Files:**
- Create: `/data2/ruanjianhao/AOrchestra/aorchestra/prompts/claweval.py`
- Create: `/data2/ruanjianhao/AOrchestra/tests/runtime/test_claweval_prompt.py`

**Interfaces:**
- Consumes: `format_tools_description` (from `aorchestra.prompts.gaia`), `build_model_pricing_table` (from `aorchestra.main_agent`).
- Produces: `class ClawEvalMainAgentPrompt` with `@staticmethod build_prompt(instruction, meta, prior_context, attempt_index, max_attempts, sub_models, subtask_history="", model_to_alias=None, tools=None) -> str`.

- [ ] **Step 1: Write the failing test**

Create `/data2/ruanjianhao/AOrchestra/tests/runtime/test_claweval_prompt.py`:

```python
"""Unit tests for ClawEvalMainAgentPrompt.

These tests don't hit an LLM. They render the prompt with synthetic inputs
and assert it carries the three-action structure + full-context delegation
guidance + interface parity with GAIAMainAgentPrompt.
"""
from __future__ import annotations

import inspect

from aorchestra.prompts.claweval import ClawEvalMainAgentPrompt
from aorchestra.prompts.gaia import GAIAMainAgentPrompt


class _FakeTool:
    def __init__(self, name, description, parameters=None):
        self.name = name
        self.description = description
        self.parameters = parameters or {}


def _render():
    return ClawEvalMainAgentPrompt.build_prompt(
        instruction="Help me organize and submit the February expense report.",
        meta={},
        prior_context="",
        attempt_index=0,
        max_attempts=10,
        sub_models=["claude-sonnet-4-5"],
        subtask_history="",
        model_to_alias=None,
        tools=[
            _FakeTool("finance_list_transactions", "List expense transactions"),
            _FakeTool("finance_submit_report", "Submit an expense report"),
        ],
    )


def test_interface_parity_with_gaia():
    """build_prompt signature must match GAIA's exactly (MainAgent calls by kw)."""
    sig_claweval = inspect.signature(ClawEvalMainAgentPrompt.build_prompt)
    sig_gaia = inspect.signature(GAIAMainAgentPrompt.build_prompt)
    assert list(sig_claweval.parameters) == list(sig_gaia.parameters)


def test_renders_to_nonempty_string():
    out = _render()
    assert isinstance(out, str)
    assert len(out) > 200


def test_offers_three_actions():
    out = _render()
    # self-execute, delegate, complete
    assert "delegate_task" in out
    assert "complete" in out
    # the self-execute path must invite calling a business tool directly
    assert "yourself" in out.lower() or "do it yourself" in out.lower()


def test_self_execute_path_present():
    out = _render()
    # The output JSON template must show an action that is a business tool
    # name placeholder, not only delegate_task/complete.
    assert "exact_tool_name" in out or "business_tool" in out or "<tool" in out.lower()


def test_delegation_demands_full_context():
    out = _render()
    low = out.lower()
    # information-island framing + "include everything" guidance
    assert "information island" in low or "cannot see" in low
    assert "hidden" in low  # hidden constraints called out
    assert "context" in low


def test_includes_question_and_tools():
    out = _render()
    assert "February expense report" in out
    assert "finance_submit_report" in out  # tool surfaced as callable
    assert "finance_list_transactions" in out


def test_preserves_budget_and_progress_sections():
    out = _render()
    assert "Attempt 0/10" in out or "Attempt 0" in out
    assert "Remaining" in out
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_claweval_prompt.py -p no:quadrants --tb=short
```

Expected: collection/import error — `aorchestra.prompts.claweval` does not exist yet.

- [ ] **Step 3: Implement `claweval.py`**

Create `/data2/ruanjianhao/AOrchestra/aorchestra/prompts/claweval.py`:

```python
"""
claw-eval-specific MainAgent prompt.

Unlike GAIA (which treats MainAgent as a pure orchestrator), claw-eval
tasks are often single-shot or single-service. MainAgent here can ALSO
execute business tools directly for simple tasks, and must pack the full
task-relevant context into the sub-agent's `context` when it delegates —
the sub-agent is an information island that sees nothing but what MainAgent
writes.

Interface-identical to GAIAMainAgentPrompt so MainAgent's keyword call at
aorchestra/main_agent.py works unchanged.
"""
from typing import Any, Dict, List

from aorchestra.main_agent import build_model_pricing_table
from aorchestra.prompts.gaia import format_tools_description


class ClawEvalMainAgentPrompt:
    """Generate MainAgent prompts for claw-eval tasks (self-execute + delegate)."""

    @staticmethod
    def build_prompt(
        instruction: str,
        meta: Dict[str, Any],
        prior_context: str,
        attempt_index: int,
        max_attempts: int,
        sub_models: List[str],
        subtask_history: str = "",
        model_to_alias: Dict[str, str] = None,
        tools: List[Any] = None,
    ) -> str:
        remaining_attempts = max_attempts - attempt_index
        model_pricing_table = build_model_pricing_table(sub_models, model_to_alias)
        tools_description = format_tools_description(tools or [])

        return f"""
You are the MainAgent. You solve the QUESTION below. You have THREE ways to act:

A. DO IT YOURSELF — call a business tool from AVAILABLE TOOLS directly. These
   tools are callable BY YOU, not only delegatable. Prefer this when the task
   is simple: finishable in a few direct tool calls, no sub-task decomposition
   needed, and you can hold the full requirement in mind.

B. DELEGATE — use 'delegate_task' for work that is complex (multiple sub-steps,
   needs a plan/decomposition), purely mechanical/long, or benefits from a
   dedicated sub-agent or a different model.

C. COMPLETE — use 'complete' once you have the answer (whether you got it
   yourself or a sub-agent did).

JUDGING SIMPLE vs COMPLEX (you decide — no fixed rule):
- Simple -> do it yourself: "submit this report", "look up X and answer",
  single-service lookups, anything that is roughly a handful of tool calls.
- Complex -> delegate: multi-service workflows, anything needing a plan, or
  where a focused sub-agent with a scoped instruction clearly helps.
- When unsure and the task is short: prefer doing it yourself. It avoids the
  delegation round-trip and keeps the full original context in your hands.

DECISION PROCESS:
1. REVIEW the SUBTASK HISTORY below (status / result / key findings).
2. EVALUATE: do the results so far SUFFICIENTLY answer the QUESTION?
3. DECIDE: do it yourself (A) / delegate (B) / complete (C).

WHEN YOU DELEGATE — GIVE THE SUB-AGENT EVERYTHING IT NEEDS:
The sub-agent is an information island. It sees ONLY the task_instruction +
the context you write — NOT the original QUESTION, NOT your reasoning, NOT
prior tool results unless you pass them. Whatever it needs to do the job
correctly, you must put in `context`. Be generous, not terse. Include:
1. The original QUESTION's relevant requirements — especially HIDDEN ones
   (de-duplication, edge cases, anomalies, "exclude X", exact totals,
   ordering, formatting constraints).
2. Concrete data / findings from prior attempts or your own tool calls that
   the sub-agent will need (IDs, values, names, partial results).
3. What "done correctly" looks like — the acceptance criteria, output format,
   any verification the sub-agent should do before finishing.
Do NOT assume the sub-agent will re-derive anything. If in doubt, include it.

BUDGET AWARENESS:
- You have LIMITED attempts (see Progress). Make each one count.
- Delegation costs a round-trip; doing simple work yourself is cheaper.
- If a result looks correct and was verified, trust it and complete.

==== MODEL SELECTION GUIDE ====
{model_pricing_table}
Note: Higher-priced models are generally more capable.

==== Progress ====
[Attempt {attempt_index}/{max_attempts}] Remaining {remaining_attempts} attempts
⚠️ Budget is limited.

==== QUESTION ====
{instruction}

==== SUBTASK HISTORY ====
{subtask_history if subtask_history else "No subtasks completed yet."}

==== AVAILABLE TOOLS ====
{tools_description}

==== OUTPUT ====
ANSWER FORMAT: precise, concise answers (single word, number, or short
phrase). Do NOT include explanations in the answer field.

Return JSON — exactly ONE of:

If DOING IT YOURSELF (call a business tool directly):
{{
  "action": "<exact_tool_name_from_AVAILABLE_TOOLS>",
  "reasoning": "This is simple enough to do directly because [X]",
  "params": {{ "...": "the tool's parameters" }}
}}

If DELEGATING:
{{
  "action": "delegate_task",
  "reasoning": "This needs decomposition/multi-step work because [X]",
  "params": {{
    "task_instruction": "A SPECIFIC, ACTIONABLE subtask",
    "context": "EVERYTHING the sub-agent needs and cannot see otherwise: relevant requirements from the QUESTION (including hidden constraints like dedup/exclusions/exact totals), concrete data and prior findings (IDs, values), and the acceptance criteria for 'done correctly'. Err on the side of MORE context.",
    "model": "one of {sub_models}",
    "tools": ["tool1", "tool2", "..."]
  }}
}}

If COMPLETE:
{{
  "action": "complete",
  "reasoning": "The results show [X], which answers the question",
  "params": {{ "answer": "concise answer" }}
}}
""".strip()
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd /data2/ruanjianhao/AOrchestra
python -m pytest tests/runtime/test_claweval_prompt.py -p no:quadrants --tb=short
```

Expected: 7 passed.

If `test_interface_parity_with_gaia` fails, the parameter list diverged — align `claweval.py`'s signature to GAIA's exactly. If `test_self_execute_path_present` fails, the JSON template's self-execute branch wording lost the `<exact_tool_name_from_AVAILABLE_TOOLS>` placeholder — restore it.

- [ ] **Step 5: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
GIT_AUTHOR_NAME="Aurorra1123" GIT_AUTHOR_EMAIL="Aurorra1123@users.noreply.github.com" \
GIT_COMMITTER_NAME="Aurorra1123" GIT_COMMITTER_EMAIL="Aurorra1123@users.noreply.github.com" \
git -C /data2/ruanjianhao/AOrchestra add aorchestra/prompts/claweval.py tests/runtime/test_claweval_prompt.py
GIT_AUTHOR_NAME="Aurorra1123" GIT_AUTHOR_EMAIL="Aurorra1123@users.noreply.github.com" \
GIT_COMMITTER_NAME="Aurorra1123" GIT_COMMITTER_EMAIL="Aurorra1123@users.noreply.github.com" \
git -C /data2/ruanjianhao/AOrchestra commit -m "feat(prompts): ClawEvalMainAgentPrompt — MainAgent self-execute + full-context delegate

claw-eval-specific MainAgent prompt with three actions: do-it-yourself
(call a business tool directly for simple tasks), delegate (with full
task-relevant context packed into the sub-agent's context), and complete.
Fixes the T012-style failure where the delegation chain flattened a hidden
dedup constraint to 0.00. Interface-identical to GAIAMainAgentPrompt;
GAIA/TB/SWE and the SubAgent prompt are untouched."
```

## Task 2: Export `ClawEvalMainAgentPrompt`

**Files:**
- Modify: `/data2/ruanjianhao/AOrchestra/aorchestra/prompts/__init__.py`

**Interfaces:**
- Consumes: `ClawEvalMainAgentPrompt` from Task 1.
- Produces: `aorchestra.prompts.ClawEvalMainAgentPrompt` importable.

- [ ] **Step 1: Update `__init__.py`**

Replace `/data2/ruanjianhao/AOrchestra/aorchestra/prompts/__init__.py` with:

```python
"""Prompts for different benchmarks."""
from aorchestra.prompts.gaia import GAIAMainAgentPrompt
from aorchestra.prompts.terminalbench import TerminalBenchPrompt
from aorchestra.prompts.swebench import SWEBenchMainAgentPrompt
from aorchestra.prompts.claweval import ClawEvalMainAgentPrompt

__all__ = [
    "GAIAMainAgentPrompt",
    "TerminalBenchPrompt",
    "SWEBenchMainAgentPrompt",
    "ClawEvalMainAgentPrompt",
]
```

- [ ] **Step 2: Verify the import**

```bash
cd /data2/ruanjianhao/AOrchestra
python -c "from aorchestra.prompts import ClawEvalMainAgentPrompt; print('import ok')"
```

Expected: `import ok`.

- [ ] **Step 3: Commit**

```bash
cd /data2/ruanjianhao/AOrchestra
GIT_AUTHOR_NAME="Aurorra1123" GIT_AUTHOR_EMAIL="Aurorra1123@users.noreply.github.com" \
GIT_COMMITTER_NAME="Aurorra1123" GIT_COMMITTER_EMAIL="Aurorra1123@users.noreply.github.com" \
git -C /data2/ruanjianhao/AOrchestra add aorchestra/prompts/__init__.py
GIT_AUTHOR_NAME="Aurorra1123" GIT_AUTHOR_EMAIL="Aurorra1123@users.noreply.github.com" \
GIT_COMMITTER_NAME="Aurorra1123" GIT_COMMITTER_EMAIL="Aurorra1123@users.noreply.github.com" \
git -C /data2/ruanjianhao/AOrchestra commit -m "feat(prompts): export ClawEvalMainAgentPrompt"
```

## Task 3: Wire claw-eval `_runner.py` to use the new prompt

**Files:**
- Modify: `/data2/ruanjianhao/claw-eval/src/claw_eval/harnesses/aorchestra/_runner.py:431,439`

**Interfaces:**
- Consumes: `ClawEvalMainAgentPrompt` (Task 2).
- Produces: claw-eval's AO MainAgent now builds prompts via `ClawEvalMainAgentPrompt`.

- [ ] **Step 1: Inspect the current construction site**

```bash
cd /data2/ruanjianhao/claw-eval
sed -n '428,445p' src/claw_eval/harnesses/aorchestra/_runner.py
```

You should see `from aorchestra.prompts.gaia import GAIAMainAgentPrompt` (~line 431) and `prompt_builder=GAIAMainAgentPrompt,` (~line 439) inside the `MainAgent(...)` construction.

- [ ] **Step 2: Make the edits**

Change the import line from:
```python
        from aorchestra.prompts.gaia import GAIAMainAgentPrompt
```
to:
```python
        from aorchestra.prompts import ClawEvalMainAgentPrompt
```

Change the constructor line from:
```python
            prompt_builder=GAIAMainAgentPrompt,
```
to:
```python
            prompt_builder=ClawEvalMainAgentPrompt,
```

Leave everything else (the `benchmark_type="gaia"` for DelegateTaskTool, the comment) as-is — only the MainAgent prompt builder changes.

- [ ] **Step 3: Run the claw-eval unit suite (no regression)**

```bash
cd /data2/ruanjianhao/claw-eval
python -m pytest tests/ -p no:quadrants --tb=short 2>&1 | tail -5
```

Expected: 105 passed / 4 skipped (the current baseline). If any test fails, STOP and report — the prompt swap shouldn't break unit tests (they don't exercise the live MainAgent loop).

- [ ] **Step 4: Smoke-import check**

```bash
cd /data2/ruanjianhao/claw-eval
AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra python -c "
import sys; sys.path.insert(0, '/data2/ruanjianhao/AOrchestra')
from aorchestra.prompts import ClawEvalMainAgentPrompt
print('ok:', ClawEvalMainAgentPrompt.__name__)
"
```

Expected: `ok: ClawEvalMainAgentPrompt`.

- [ ] **Step 5: Commit**

```bash
cd /data2/ruanjianhao/claw-eval
git add src/claw_eval/harnesses/aorchestra/_runner.py
git commit -m "feat(aorchestra): MainAgent uses ClawEvalMainAgentPrompt (self-execute + full-context delegate)

Switch the AO MainAgent prompt builder from GAIAMainAgentPrompt to the
new claw-eval-specific ClawEvalMainAgentPrompt. MainAgent can now execute
simple tasks directly and packs full context when delegating. One-line
swap; the rest of the aorchestra harness is unchanged."
```

## Task 4: Validation run (changed prompt only) — observe behavior

**Files:**
- None (verification only).

**Interfaces:**
- Consumes: all prior tasks.
- Produces: evidence the new prompt activates self-execution and fixes/improves T012.

This validates the three acceptance criteria from the spec. Run a small set of tasks through the AO harness with the new prompt and inspect traces.

- [ ] **Step 1: Run T012 + a few simple + one complex task**

T012 has the hidden dedup constraint. T002 (email) / T008 (todo) / T018 (ticket) are simple single-service. T032_escalation_budget_triage is a multi-step workflow (should still delegate). Use the merged `--task-ids` flag (no copy/symlink needed).

```bash
cd /data2/ruanjianhao/claw-eval
source .venv/bin/activate 2>/dev/null || true
AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra \
CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1 \
CLAWEVAL_LLM_API_KEY=sk-u3HGm150NcbCJu2ohLB6BIgktG8V3QmVz8LA5JSTeDQXHhEo \
CLAWEVAL_LLM_MODEL=claude-sonnet-4-5 \
CLAWEVAL_AORCHESTRA_RUNTIME=pi \
no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1 \
python -m claw_eval.cli batch \
  --tasks-dir tasks \
  --task-ids T012_expense_report,T002_email_triage,T008_todo_management,T018_ticket_triage,T032_escalation_budget_triage \
  --harness aorchestra --config config_concurrency_smoke.yaml \
  --parallel 5 --port-base-offset 500 --trials 1 \
  --trace-dir /data2/ruanjianhao/claw-eval/traces/selfexec_validation 2>&1 | tail -30
```

Up to ~15 min (5 tasks, sonnet, pi runtime, judge enabled). Be patient (set Bash timeout 1800000 ms).

- [ ] **Step 2: Check acceptance criterion 1 — T012 escapes 0.00**

```bash
cd /data2/ruanjianhao/claw-eval
REPORT=$(find traces/selfexec_validation -name "*T012*.jsonl" | grep -v raw | head -1)
echo "T012 trace: $REPORT"
# pull the batch result score
cat traces/selfexec_validation/*/batch_results.json 2>/dev/null | python -m json.tool 2>/dev/null | grep -A3 -i "T012" | head -10
```

Expected: T012 task_score > 0.00 (any improvement is meaningful; the prior baseline was exactly 0.00). Capture the score.

- [ ] **Step 3: Check acceptance criterion 2 — a simple task self-executed**

```bash
cd /data2/ruanjianhao/claw-eval
# Inspect the MainAgent actions in a simple task's trace. Look for an action
# that is a business tool name (NOT delegate_task / complete).
for t in T002_email_triage T008_todo_management T018_ticket_triage; do
  RAW=$(find traces/selfexec_validation -path "*${t}*" -name "*.json" | grep -i raw | head -1)
  echo "=== $t raw: $RAW ==="
  python3 -c "
import json,glob
files=glob.glob('traces/selfexec_validation/**/${t}*'+'_raw/*.json', recursive=True)
import sys
for f in files:
    try: d=json.load(open(f))
    except: continue
    traj=d.get('trajectory',[])
    actions=[a.get('action') for a in traj]
    print('  actions:', actions)
    selfexec=[a for a in actions if a not in ('delegate_task','complete','error',None)]
    print('  self-executed business-tool calls:', selfexec)
" 2>/dev/null
done
```

Expected: at least one of the simple tasks shows a MainAgent action that is a business-tool name (e.g. `gmail_list_messages`, `todo_update_task`, `helpdesk_get_ticket`) — proving the self-execute path activated. If ALL three still only show `delegate_task`/`complete`, the prompt isn't biasing toward self-execution enough — note it for follow-up (a prompt-tuning iteration), don't fail the task.

- [ ] **Step 4: Check acceptance criterion 3 — complex task still delegates**

```bash
cd /data2/ruanjianhao/claw-eval
RAW=$(find traces/selfexec_validation -path "*T032*" -name "*.json" | grep -i raw | head -1)
python3 -c "
import json,glob
files=glob.glob('traces/selfexec_validation/**/T032*_raw/*.json', recursive=True)
for f in files:
    try: d=json.load(open(f))
    except: continue
    actions=[a.get('action') for a in d.get('trajectory',[])]
    print('T032 actions:', actions)
    print('delegated:', 'delegate_task' in actions)
" 2>/dev/null
```

Expected: T032 still contains at least one `delegate_task` — the architecture didn't collapse into pure single-agent.

- [ ] **Step 5: Write a short validation doc**

Create `/data2/ruanjianhao/claw-eval/docs/rollout_mainagent_selfexec_validation.md` summarizing:
- The 5-task validation command + setup.
- Per-task: score, MainAgent action sequence (self-exec vs delegate vs complete).
- Acceptance criteria verdict: (1) T012 score vs prior 0.00, (2) did any simple task self-execute, (3) did T032 still delegate.
- The dedup-fix evidence: for T012, quote the relevant part of the MainAgent's chosen action (did it self-submit correctly, or delegate with dedup in context?).
- Honest read: did the change help, hurt, or no-op? Any follow-up prompt tuning needed?

```bash
cd /data2/ruanjianhao/claw-eval
git add docs/rollout_mainagent_selfexec_validation.md
git commit -m "docs: MainAgent self-execute validation run (T012 + simple + complex)"
```

- [ ] **Step 6: Report findings to the human**

Report: T012 score (vs 0.00 baseline), whether self-execute activated on simple tasks, whether complex task still delegated, and whether a prompt-tuning iteration is warranted before scaling to a full 50-task AO run.

---

## Self-Review

**1. Spec coverage:**
- Spec §3 (归属/接入) → Tasks 1+2 (new prompt in AOrchestra) + Task 3 (one-line _runner switch). ✓
- Spec §4.1 (三动作 + 判断原则) → Task 1's prompt body (A/B/C + JUDGING SIMPLE vs COMPLEX). ✓
- Spec §4.2 (完整 context 打包) → Task 1's "WHEN YOU DELEGATE — GIVE THE SUB-AGENT EVERYTHING IT NEEDS" block + the context-field guidance in the JSON template. ✓
- Spec §4.3 (三选一 JSON) → Task 1's OUTPUT section. ✓
- Spec §4.4 (保留 budget/model/progress/etc.) → Task 1 keeps all GAIA sections. ✓
- Spec §5 (不改 SubAgent / 不动执行层 / 隔离 GAIA) → Global Constraints + no tasks touch those. ✓
- Spec §6 (只跑改后验证, 三判据) → Task 4 (T012 escapes 0, simple self-exec, complex still delegates). ✓

**2. Placeholder scan:** No TBD/TODO. All code blocks complete. The prompt text is the full f-string. The validation inspection scripts are complete Python one-liners. ✓

**3. Type consistency:** `build_prompt` signature is identical across Task 1's implementation, the GAIA reference, and Task 1's parity test. `format_tools_description` / `build_model_pricing_table` import paths match gaia.py. The `_runner.py` edit changes only the import + `prompt_builder=` line. ✓

No issues found.
