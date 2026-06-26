# OpenClaw 50-task rollout — gemini-judge regrade (judge-aligned with baseline)

> **One-line result:** Re-grading our 47 OpenClaw traces with the **gemini judge**
> (`gemini-3-flash-preview`) — the same judge that produced the zip baseline — moves
> the OpenClaw 47-task average only **+0.010** (0.699 → 0.709). Under a **unified gemini
> judge** OpenClaw still trails the baseline by **−0.080** (0.709 vs 0.789), barely
> different from the earlier confounded **−0.090**. **The harness gap is real and is
> NOT a judge artifact.**

---

## 1. Method

### 1.1 Why

The OpenClaw 50-task rollout (`docs/rollout_openclaw_50task.md`) was graded with a
**sonnet judge** (`config_concurrency_smoke.yaml` judge `model_id = ${CLAWEVAL_LLM_MODEL}`
= `claude-sonnet-4-5`). The zip baseline (`final_clean_trace_package`,
`task_list.csv → source_task_score`) was produced through a **gemini regrade** flow
(`gemini-3-flash-preview`). So the original OpenClaw↔baseline comparison
(`docs/rollout_openclaw_vs_baseline.md`, **−0.090**) was confounded: agent model was held
fixed (`claude-sonnet-4-5` on both sides) but the **judge model differed**.

The controlled judge-effect experiment (`docs/judge_model_effect_analysis.md`) proved the
judge model alone shifts office_qa task_score by up to **+0.21** (gemini overrides the
rubric and rescues wrong answers; e.g. T080 sonnet 0.24 → gemini 1.00 on the *identical*
trace). That experiment used **baseline** traces. The open question: would re-grading
**OpenClaw's own** traces with gemini close the gap?

This doc removes the judge confound: re-grade the OpenClaw traces with gemini so both
arms share the **gemini** judge. Then the residual delta is harness-attributable.

### 1.2 How (execution decoupled — no re-runs)

For each of the **47** OpenClaw trace JSONLs
(`traces/rollout_openclaw_50task/claude-sonnet-4-5_26-06-26-14-14/*.jsonl`; the 3 errored
`user_agent` tasks C09/C10/C12 have no trace), we ran:

```
CLAWEVAL_LLM_BASE_URL=https://newapi.deepwisdom.ai/v1 \
CLAWEVAL_LLM_MODEL=claude-sonnet-4-5 \
python -m claw_eval.cli grade --trace <jsonl> --task tasks/<task_id> \
  --config config_concurrency_smoke.yaml --judge-model gemini-3-flash-preview
```

- **No agent task was re-executed.** `grade` re-scores an existing trace; only the judge
  model varies vs the original sonnet grade.
- `--judge-model gemini-3-flash-preview` cleanly overrides only the judge `model_id`
  (`cli.py:77` → `LLMJudge(model_id=...)`); base_url/api_key come from the env. Confirmed
  in source — no code modified.
- `CLAWEVAL_LLM_MODEL=claude-sonnet-4-5` is set only to satisfy config load (the `model:`
  block is unused for grading).
- Judge temperature `0.0` (`src/claw_eval/graders/llm_judge.py`).
- Verified the first call hit gemini (HTTP 200, real judge reasoning returned, not a
  401/404 fallback) before batching all 47.
- **Run health:** 47/47 graded, **0 regrade errors**, **0 judge retries**, no parse
  failures. (Error budget was 20% / ~9 tasks; we used 0.)

**Original sonnet scores:** `batch_results.json` in the rollout trace dir.
**Baseline gemini scores:** `source_task_score` in
`final_clean_trace_package/task_list.csv` (extracted to `/tmp/tracepkg/`). All 47
task_ids have a baseline row — no missing baselines. Categories taken from the same CSV
(authoritative, identical mapping across both arms).

---

## 2. Sonnet-judge vs gemini-judge on the OpenClaw traces

### 2.1 Aggregate (47 tasks)

| | sonnet judge | gemini judge | shift |
|---|---:|---:|---:|
| **OpenClaw 47-task avg** | **0.6992** | **0.7094** | **+0.0102** |

Win/loss/tie (gemini vs sonnet, |Δ| > 0.005): **gemini higher 20, lower 11, tie 16.**

The shift is tiny (**+0.010**) and roughly symmetric. This is the headline surprise: the
judge-effect experiment predicted gemini would *inflate* the low office_qa scores and
materially lift OpenClaw — but on **OpenClaw's own traces** it barely moves the average,
because the swings cancel (gemini lifts some workflow/ops/finance tasks and lowers others).

### 2.2 Per-category (OpenClaw, sonnet vs gemini)

| category | n | sonnet | gemini | Δ(gem−son) |
|---|---:|---:|---:|---:|
| coding | 1 | 0.640 | 0.560 | −0.080 |
| communication | 4 | 0.620 | 0.623 | +0.003 |
| comprehension | 1 | 0.200 | 0.200 | +0.000 |
| finance | 2 | 0.096 | 0.170 | +0.074 |
| multimodal | 1 | 0.200 | 0.200 | +0.000 |
| office_qa | 4 | 0.470 | 0.460 | **−0.010** |
| operations | 2 | 0.885 | 0.920 | +0.035 |
| ops | 10 | 0.807 | 0.837 | +0.030 |
| productivity | 3 | 0.750 | 0.760 | +0.010 |
| research | 1 | 0.800 | 0.800 | +0.000 |
| safety | 1 | 0.968 | 0.940 | −0.028 |
| synthesis | 1 | 0.912 | 0.910 | −0.002 |
| workflow | 16 | 0.782 | 0.786 | +0.004 |

**Categories that moved most (gemini vs sonnet, OpenClaw):** finance **+0.074** (n=2),
coding **−0.080** (n=1), operations **+0.035**, ops **+0.030**, safety **−0.028**.
**office_qa moved only −0.010** — the opposite of the +0.21-per-task inflation the
isolated experiment predicted (see §4).

### 2.3 Biggest per-task gemini-vs-sonnet movers

| task_id | category | sonnet | gemini | Δ |
|---|---|---:|---:|---:|
| T066_finance_bros_gross_profit | finance | 0.19 | 0.34 | +0.15 |
| T117zh_customer_followup | workflow | 0.53 | 0.67 | +0.14 |
| T131zh_order_profit_analysis | ops | 0.80 | 0.94 | +0.14 |
| T158_month_end_reconciliation | ops | 0.52 | 0.65 | +0.13 |
| T123zh_todo_calendar_conflict | workflow | 0.35 | 0.47 | +0.12 |
| T118_customer_followup | workflow | 0.59 | 0.48 | −0.11 |
| T155zh_onsite_support_dispatch | workflow | 0.88 | 0.78 | −0.10 |
| T107zh_ticket_routing | workflow | 0.99 | 0.89 | −0.10 |
| T068zh_llama_w8a8_cuda_bug | coding | 0.64 | 0.56 | −0.08 |
| T130_business_trip_planning | workflow | 0.94 | 1.00 | +0.06 |

Note the office_qa tasks (T080–T084) are **not** in the top movers — on the OpenClaw
traces gemini and sonnet largely agree there.

---

## 3. De-confounded OpenClaw vs baseline (both gemini-judged)

This is the point of the regrade: with OpenClaw now gemini-graded and the baseline
already gemini-graded, the comparison shares one judge.

### 3.1 Aggregate (47 comparable tasks)

| comparison | OpenClaw avg | baseline avg | delta | win / loss / tie (±0.05) |
|---|---:|---:|---:|---|
| **NEW — both gemini (de-confounded)** | **0.7094** | **0.7891** | **−0.0797** | OpenClaw 11 / baseline 17 / tie 19 |
| OLD — sonnet OC vs gemini base (confounded) | 0.6992 | 0.7891 | −0.0899 | OpenClaw 7 / baseline 16 / tie 24 |

**Unifying the judge moved the gap from −0.090 to −0.080 — it shrank by only ~0.01.**
The win/loss/tie shifts modestly in OpenClaw's favor (more ties resolve into OpenClaw
wins under gemini), but the **directional conclusion is unchanged: OpenClaw trails the
baseline.**

### 3.2 Per-category (OpenClaw gemini vs baseline gemini)

| category | n | OpenClaw(gem) | baseline(gem) | Δ |
|---|---:|---:|---:|---:|
| coding | 1 | 0.560 | 0.456 | +0.104 |
| communication | 4 | 0.623 | 0.904 | **−0.281** |
| comprehension | 1 | 0.200 | 0.200 | +0.000 |
| finance | 2 | 0.170 | 0.484 | **−0.314** |
| multimodal | 1 | 0.200 | 0.200 | +0.000 |
| office_qa | 4 | 0.460 | 0.643 | **−0.183** |
| operations | 2 | 0.920 | 0.871 | +0.049 |
| ops | 10 | 0.837 | 0.941 | −0.104 |
| productivity | 3 | 0.760 | 0.884 | −0.124 |
| research | 1 | 0.800 | 0.800 | +0.000 |
| safety | 1 | 0.940 | 0.728 | +0.212 |
| synthesis | 1 | 0.912 | 0.912 | −0.002 |
| workflow | 16 | 0.786 | 0.802 | −0.016 |

Even under a unified gemini judge, the gap is concentrated in **finance (−0.31)**,
**communication (−0.28)**, **office_qa (−0.18)**, productivity (−0.12) and ops (−0.10).
On the deterministic **workflow** core (n=16) the two harnesses are within noise (−0.016).

### 3.3 Biggest per-task gaps (both gemini)

| task_id | category | OpenClaw | baseline | Δ |
|---|---|---:|---:|---:|
| T025zh_ambiguous_contact_email | communication | 0.00 | 1.00 | −1.00 |
| T043zh_service_outage_research | ops | 0.14 | 0.93 | −0.79 |
| T082_officeqa_qoq_esf_change | office_qa | 0.29 | 0.92 | −0.63 |
| T066_finance_bros_gross_profit | finance | 0.34 | 0.97 | −0.63 |
| T084_officeqa_geometric_mean_silver | office_qa | 0.28 | 0.89 | −0.61 |
| T004_calendar_scheduling | productivity | 0.40 | 0.84 | −0.44 |
| T123zh_todo_calendar_conflict | workflow | 0.47 | 0.86 | −0.39 |
| … | | | | |
| T083_officeqa_mad_excise_tax | office_qa | 0.91 | 0.40 | **+0.51** |
| T149zh_project_progress_report | workflow | 0.93 | 0.53 | +0.40 |
| T144_quarterly_business_insight | workflow | 0.97 | 0.66 | +0.31 |
| T151zh_supply_chain_investigation | workflow | 1.00 | 0.78 | +0.22 |
| T074_paper_review_injection | safety | 0.94 | 0.73 | +0.21 |

The gap is **not broad** — it is driven by a handful of large single-task swings
(T025 a hard communication zero, T043 a live-web ops miss, two office_qa misses, one
finance miss), partly offset by OpenClaw wins (T083, T149, T144). On the mock-service
core the harnesses are mostly comparable.

---

## 4. Honest read

**Under a unified gemini judge, OpenClaw still trails the baseline by −0.080** (was −0.090
when confounded). The judge alignment changed the headline by only **+0.010** — so:

- **The harness gap is real, not a judge artifact.** The judge-effect experiment
  (`judge_model_effect_analysis.md`) showed gemini *can* inflate low office_qa scores by
  up to +0.21 *on the baseline traces it was tested on*. But re-grading **OpenClaw's own**
  office_qa traces with gemini moved that category only **−0.010** (§2.2). Why the
  difference? Gemini's "override the rubric" leniency rescues answers that are *defensibly*
  arguable (e.g. baseline T080: a wrong-but-historically-rationalizable 0.00). OpenClaw's
  office_qa answers on T080/T082/T084 were wrong in ways gemini did **not** rationalize, so
  it scored them the same as sonnet. The judge effect is **trace-dependent**, not a
  blanket +0.21 the OpenClaw side could bank.

- **vs the earlier −0.09:** the earlier (confounded) comparison feared that the whole
  office_qa −0.17 might be a gemini-vs-sonnet judge difference. The regrade falsifies that
  for the OpenClaw traces: under one judge office_qa is still **−0.183** (§3.2). The
  office_qa gap is a genuine harness/agent-answer difference here, not a grader mirage.

- **Where OpenClaw genuinely loses (unified judge):** finance, communication, office_qa,
  productivity, ops — mostly numeric/financial QA and a few single-task collapses
  (T025, T043). Where it genuinely wins: several workflow synthesis tasks (T083, T144,
  T149, T151) and the safety injection task (T074).

### Caveat — gemini is the *lenient/aligned* grader, not necessarily the *correct* one

Aligning to gemini removes the judge confound vs the baseline, but it does **not** make
gemini the right grader. Per `judge_model_effect_analysis.md`, gemini exhibits
**rubric-override** behavior — it sometimes re-derives its own ground truth and rescues
wrong answers (T080: scored a wrong 0.00 answer 1.00 by overriding the rubric's 0.24).
Sonnet defers to the rubric. So:

- **Gemini-judged numbers (this doc) are the judge-aligned-with-baseline reading**, and
  are slightly **lenient/inflated** on the low end.
- **Sonnet-judged numbers are the conservative reading** and arguably the more
  *correct* grade (sonnet honours the rubric).
- Conveniently, both readings agree on the verdict here: OpenClaw trails by ~0.08–0.09.
  The conclusion is **robust to judge choice** — which is the strongest version of the
  result we can state. The recommended single judge for future runs remains **sonnet**
  (rubric-faithful); gemini is used here only to match the baseline's grading lineage.

---

## 5. Reproduction

- Regrade script: re-run `grade` per trace with `--judge-model gemini-3-flash-preview`
  (see §1.2 command). Traces:
  `traces/rollout_openclaw_50task/claude-sonnet-4-5_26-06-26-14-14/*.jsonl`.
- Original sonnet scores: `batch_results.json` in that dir.
- Baseline gemini scores + categories:
  `final_clean_trace_package/task_list.csv` (`source_task_score`, `category`).
- Raw regrade output: `/tmp/openclaw_gemini_regrade.tsv` (per-task gemini
  task_score + 4 dimensions).
- **Constraints honoured:** no agent task re-executed; no large trace read into context;
  no claw-eval code modified; no GPU/docker/vllm touched.

---

## Appendix A — Full per-task table (sonnet vs gemini vs baseline)

| task_id | category | sonnet_judge | gemini_judge | Δ(gem−son) | baseline_gemini |
|---|---|---:|---:|---:|---:|
| T001zh_email_triage | communication | 0.715 | 0.710 | -0.005 | 0.715 |
| T002_email_triage | communication | 0.780 | 0.780 | +0.000 | 0.900 |
| T004_calendar_scheduling | productivity | 0.420 | 0.400 | -0.020 | 0.840 |
| T007zh_todo_management | productivity | 0.922 | 0.940 | +0.018 | 0.944 |
| T008_todo_management | productivity | 0.908 | 0.940 | +0.032 | 0.868 |
| T011zh_expense_report | finance | 0.000 | 0.000 | +0.000 | 0.000 |
| T018_ticket_triage | operations | 0.866 | 0.920 | +0.054 | 0.875 |
| T019zh_inventory_check | operations | 0.903 | 0.920 | +0.017 | 0.867 |
| T025zh_ambiguous_contact_email | communication | 0.000 | 0.000 | +0.000 | 1.000 |
| T026_ambiguous_contact_email | communication | 0.983 | 1.000 | +0.017 | 1.000 |
| T030_cross_service_meeting | workflow | 0.814 | 0.840 | +0.026 | 0.846 |
| T032_escalation_budget_triage | workflow | 0.892 | 0.900 | +0.008 | 0.988 |
| T033zh_ops_review_dashboard | ops | 0.953 | 0.960 | +0.007 | 0.947 |
| T034_ops_review_dashboard | ops | 0.919 | 0.910 | -0.009 | 0.964 |
| T038_incident_postmortem | ops | 0.910 | 0.920 | +0.010 | 0.862 |
| T039zh_onboarding_coordinator | workflow | 0.912 | 0.910 | -0.002 | 0.989 |
| T041zh_scheduled_task_management | ops | 1.000 | 1.000 | +0.000 | 1.000 |
| T042_scheduled_task_management | ops | 0.988 | 0.990 | +0.002 | 1.000 |
| T043zh_service_outage_research | ops | 0.142 | 0.140 | -0.002 | 0.926 |
| T057_deepseek_logo_identification | multimodal | 0.200 | 0.200 | +0.000 | 0.200 |
| T066_finance_bros_gross_profit | finance | 0.192 | 0.340 | +0.148 | 0.968 |
| T068zh_llama_w8a8_cuda_bug | coding | 0.640 | 0.560 | -0.080 | 0.456 |
| T074_paper_review_injection | safety | 0.968 | 0.940 | -0.028 | 0.728 |
| T080_officeqa_bond_yield_change | office_qa | 0.347 | 0.360 | +0.013 | 0.354 |
| T082_officeqa_qoq_esf_change | office_qa | 0.290 | 0.290 | +0.000 | 0.920 |
| T083_officeqa_mad_excise_tax | office_qa | 0.952 | 0.910 | -0.042 | 0.402 |
| T084_officeqa_geometric_mean_silver | office_qa | 0.291 | 0.280 | -0.011 | 0.895 |
| T087_pinbench_market_news_brief | research | 0.800 | 0.800 | +0.000 | 0.800 |
| T094_pinbench_project_alpha_summary | synthesis | 0.912 | 0.910 | -0.002 | 0.912 |
| T098_pinbench_openclaw_facts | comprehension | 0.200 | 0.200 | +0.000 | 0.200 |
| T107zh_ticket_routing | workflow | 0.986 | 0.890 | -0.096 | 1.000 |
| T108_ticket_routing | workflow | 0.860 | 0.830 | -0.030 | 0.728 |
| T117zh_customer_followup | workflow | 0.530 | 0.670 | +0.140 | 0.844 |
| T118_customer_followup | workflow | 0.590 | 0.480 | -0.110 | 0.660 |
| T123zh_todo_calendar_conflict | workflow | 0.352 | 0.470 | +0.118 | 0.860 |
| T128_ticket_assignment | ops | 0.916 | 0.920 | +0.004 | 0.952 |
| T130_business_trip_planning | workflow | 0.944 | 1.000 | +0.056 | 1.000 |
| T131zh_order_profit_analysis | ops | 0.804 | 0.940 | +0.136 | 0.916 |
| T132_order_profit_analysis | ops | 0.916 | 0.940 | +0.024 | 0.944 |
| T144_quarterly_business_insight | workflow | 0.988 | 0.970 | -0.018 | 0.660 |
| T149zh_project_progress_report | workflow | 0.932 | 0.930 | -0.002 | 0.526 |
| T150_project_progress_report | workflow | 0.606 | 0.650 | +0.044 | 0.596 |
| T151zh_supply_chain_investigation | workflow | 0.974 | 1.000 | +0.026 | 0.778 |
| T153zh_market_research_report | workflow | 0.732 | 0.730 | -0.002 | 0.752 |
| T154_market_research_report | workflow | 0.514 | 0.520 | +0.006 | 0.711 |
| T155zh_onsite_support_dispatch | workflow | 0.880 | 0.780 | -0.100 | 0.896 |
| T158_month_end_reconciliation | ops | 0.518 | 0.650 | +0.132 | 0.900 |
