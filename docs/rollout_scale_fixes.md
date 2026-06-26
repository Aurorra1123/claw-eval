# Scale-Readiness Fixes — claw-eval batch ergonomics

**Date:** 2026-06-26
**Branch:** `main`
**Context:** Three ergonomic blockers surfaced during the 5-task concurrent sonnet
rollout (`docs/rollout_ao_pi_5task.md`, `docs/rollout_openclaw_5task.md`,
`docs/rollout_harness_ab_5task_summary.md`). All three blocked scaling the
OpenClaw-vs-AOrchestra harness comparison to larger arbitrary task sets. Fixed
here, each with a test + regression run + its own commit.

Baseline before: **94 passed / 4 skipped**. After: **105 passed / 4 skipped**
(+11 new tests). `python -m pytest tests/ -p no:quadrants --tb=short`.

---

## Fix 1 — `batch --task-ids` for arbitrary non-contiguous selection

**Commit:** `bd2a7fa`
**Files:** `src/claw_eval/cli.py` (`cmd_batch` selection + argparse),
`tests/test_cli_batch_task_ids.py` (5 tests)

**Problem.** `cmd_batch` supported only `--filter` (single substring), `--tag`,
and `--range` (contiguous numeric). Selecting `{T002, T008, T012, T018, T077}`
(non-contiguous) was impossible, so both rollout subagents copied/symlinked the
5 task dirs into a sibling dir as a workaround.

**Fix.** Added `--task-ids ID1,ID2,...`. It accepts full task-dir names
(== task_id, e.g. `T002_email_triage`) **or** the short numeric form
(`T002`). It is the **authoritative** selector:

- Mutually exclusive with `--filter` / `--tag` / `--range` — errors clearly if
  combined (`--task-ids is mutually exclusive with ...`).
- Errors with the list of **unmatched** IDs rather than silently skipping
  (`--task-ids: N requested ID(s) not found in <dir>: ...`).
- Short-form ambiguity (a `T\d+` prefix matching >1 dir) is reported as an error
  asking for the full name; in the real `tasks/` set each `T\d+` is unique.

**Verified.** Against the real `tasks/` dir, both
`--task-ids T002_email_triage,T008_todo_management,T012_expense_report,T018_ticket_triage,T077_officeqa_highest_dept_spending`
and the short `--task-ids T002,T008,T012,T018,T077` select **exactly the 5
tasks**, with no copy/symlink dir.

---

## Fix 2 — `_resolve_tasks_dir` resolves symlinked task dirs

**Commit:** `ee5704a`
**Files:** `src/claw_eval/cli.py` (`_resolve_tasks_dir`),
`tests/test_cli_resolve_tasks_dir.py` (2 tests)

**Problem.** `_resolve_tasks_dir` derived the tasks-root via
`task_yaml.parent.parent` **without** `.resolve()`. For a symlinked task dir
(`tmp/T002 -> repo/tasks/T002`), `parent.parent` was `tmp` (the symlink's
parent), so callers using `tasks_dir.parent` as the mock-service CWD landed in
`/tmp` — and the relative `python mock_services/.../server.py` service commands
failed to spawn (`mock_services/` only exists at the repo root). This is why the
symlink workaround was rejected in both rollouts.

**Fix.** `return task_yaml.resolve().parent.parent`. Resolving the `task.yaml`
path first makes a symlinked task dir resolve to its real location, so
`tasks_dir.parent` lands at the real repo root. For a non-symlink path
`.resolve()` only absolutises it (`parent.parent` unchanged), so normal runs are
unaffected. Grader loading is unaffected because call sites pass
`task_dir=task_yaml.parent` (unresolved) as the grader fallback.

**Verified.** A test creates `selection/T002 -> repo/tasks/T002` (a real
symlink) with `repo/mock_services/` present, and asserts the resolved CWD is the
real repo root (`(cwd / "mock_services").exists()`), not the symlink's parent.
Demonstrated old-vs-new: old CWD `/tmp/...` (no `mock_services`), new CWD
`/tmp/.../repo` (has `mock_services`). Full suite unchanged → no regression from
`.resolve()`.

With Fix 1 in place, `--task-ids` against the real `tasks/` dir is the
recommended path and avoids symlinks entirely; Fix 2 additionally makes the
symlink approach correct if anyone still uses it.

---

## Fix 3 — graders no longer crash (or hang) on `--no-judge`

**Commits:** `5cadc43` (null-object), `dd33427` (email-triage hang follow-up)
**Files:** `src/claw_eval/graders/llm_judge.py` (`NoJudge`, `enabled` markers),
`src/claw_eval/cli.py` (`_grade_with_optional_params` chokepoint),
`tasks/T001zh_email_triage/grader.py`, `tasks/T002_email_triage/grader.py`,
`tests/test_grader_no_judge.py` (4 tests)

**Problem.** 57 task graders (the officeqa family T076-T085, plus finance/video/zh
graders) call `judge.evaluate()` / `evaluate_actions()` / `evaluate_visual()`
unconditionally. With `--no-judge`, `judge` is `None`, so grading crashed:
`AttributeError: 'NoneType' object has no attribute 'evaluate'`. This forced
every rollout to run with a judge enabled (extra cost).

**Fix (framework-level).** Added a `NoJudge` null-object whose `evaluate*`
methods return a neutral `JudgeResult(score=0.0)` and log the skip
(`no_judge=True`). It is substituted for `None` at the **single** grading
chokepoint `_grade_with_optional_params`, so all six grading call sites benefit
without patching each grader. The judge-scored dimension contributes **0.0**
(safe — not a silent pass); rule-based components still count, so completion is
partial rather than crashing or falsely passing.

**Follow-up (uncovered by the integration check).** The email-triage graders
(`T001zh_email_triage` base + `T002` variant) bypass `judge.evaluate()` and call
`judge.client.chat.completions.create()` **directly** inside a 30-attempt
exponential-backoff retry loop. With the judge disabled, `NoJudge` has no
`.client`, so the `AttributeError` fell into the retry loop and grading **HUNG**
(minutes) instead of completing — the original `judge=None` would have hung the
same way. Fix: an `enabled` class marker (`True` on `LLMJudge`, `False` on
`NoJudge`), and the two direct-client graders guard with
`if judge is None or not getattr(judge, "enabled", True): return 0.0`.

**Verified.** A test runs the real T077 officeqa grader with `judge=None` and
asserts a valid `DimensionScores` (no crash, partial completion, the skipped
judge recorded as a `no_judge` neutral call). A second test runs the
direct-client email-triage grader with `judge=None` and asserts it returns in
< 5s (retry loop short-circuited). Confirmed against the **real aorchestra T002
trace**: grades in 0.33s (was hanging), task_score 0.48.

---

## Integration check (all three together)

1 task, `--harness aorchestra` host mode, `CLAWEVAL_AORCHESTRA_RUNTIME=react`,
sonnet creds:

```bash
cd /data2/ruanjianhao/claw-eval
export CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1
export CLAWEVAL_LLM_API_KEY=...            # sonnet via deepwisdom
export CLAWEVAL_LLM_MODEL=claude-sonnet-4-5
export CLAWEVAL_AORCHESTRA_RUNTIME=react
export AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra
python -u -m claw_eval.cli batch \
  --tasks-dir tasks \
  --task-ids T002_email_triage \
  --config config_concurrency_smoke.yaml \
  --harness aorchestra --no-judge \
  --parallel 1 --port-base-offset 750 --trials 1 \
  --trace-dir /tmp/itest_traces2
```

**Result:** batch ran end-to-end, `batch_results.json` written with
`error: None`, `T002_email_triage  0.48  C=0.35 R=1.00 M=0.00 S=1`,
wall ~71s. No copy/symlink dir (Fix 1 + real-dir resolution Fix 2), and
`--no-judge` grading completed without crash or hang (Fix 3). This is the first
time `--no-judge` grading has completed for this task family.

---

## Recommended `--task-ids` command for the next scaled rollout

The copy-into-sibling-dir workaround is no longer needed. Select the 5 tasks
(or any arbitrary set) directly against the real `tasks/` dir:

```bash
python -m claw_eval.cli batch \
  --tasks-dir tasks \
  --task-ids T002_email_triage,T008_todo_management,T012_expense_report,T018_ticket_triage,T077_officeqa_highest_dept_spending \
  --harness aorchestra \
  --config config_concurrency_smoke.yaml \
  --parallel 5 --port-base-offset 500 --trials 1 \
  --trace-dir traces/<run_name>
```

(Short form `--task-ids T002,T008,T012,T018,T077` also works.) Add `--no-judge`
to skip the judge entirely now that graders tolerate it (note: judge-scored
completion dimensions then score 0.0 — keep the judge enabled if you need those
dimensions; the value of Fix 3 is that `--no-judge` no longer *crashes*).
