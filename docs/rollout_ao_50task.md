# AOrchestra (pi runtime) — 50-task rollout, new self-execute MainAgent

AO leg of the 3-way harness comparison (baseline / OpenClaw / AO). Same 50 tasks as
the OpenClaw run, run against `claude-sonnet-4-5` via the deepwisdom newapi endpoint.

## Setup

- **Harness:** `aorchestra`, runtime `pi` (Node worker per delegation, stdio, host-mode).
- **New MainAgent:** `ClawEvalMainAgentPrompt` — 3 actions (self-execute a business tool /
  delegate-with-full-context / complete). Loop fix in place: business-tool results are now
  written back into both `self.context` and `task_entries`.
  AOrchestra commits: `2b36e77` (prompt), `3865e15` + `8c694ba` (loop fix).
- **Model:** `claude-sonnet-4-5`. **Judge:** sonnet, enabled (REQUIRED — officeqa graders
  crash on `--no-judge`).
- **Concurrency:** `--parallel 8`, `--port-base-offset 500`, `--trials 1`.
- **Tasks:** 50 from `/tmp/openclaw_50_final.txt` — 43 plain mock-service + 4 sandbox/web + 3 user_agent.

Command:

```
CLAWEVAL_AORCHESTRA_RUNTIME=pi AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra \
python -m claw_eval.cli batch \
  --task-ids "<50 ids, comma-sep>" \
  --harness aorchestra --config config_concurrency_smoke.yaml \
  --port-base-offset 500 --trials 1 --parallel 8 \
  --trace-dir /data2/ruanjianhao/claw-eval/traces/rollout_ao_50task
```

Trace dir: `traces/rollout_ao_50task/claude-sonnet-4-5_26-06-26-15-52/`

## Aggregate

| metric | value |
|---|---|
| tasks | 50 |
| scored | 46 |
| errored | 4 |
| **avg score (over 50, errored=0)** | **0.693** |
| avg score (over 46 scored) | 0.753 |
| **pass@1 (over 50)** | **33/50 = 0.660** |
| pass@1 (over 46 scored) | 33/46 = 0.717 |
| batch wall-clock | ~17.7 min (15:52:41 → 16:10:21) |
| sum per-task wall | 6248 s (104 min, summed not parallel) |
| **total cost** | **$2.33** (AO `total_cost`; claw-eval shows 0 tokens because AO uses its own LLM client) |

## Per-category average

| category | n (scored) | avg | pass | errored |
|---|---|---|---|---|
| mock-service | 35 | 0.786 | 27/35 | 0 |
| officeqa | 4 | 0.704 | 3/4 | 0 |
| pinbench | 4 | 0.670 | 2/4 | 0 |
| sandbox/web | 3 | 0.545 | 1/3 | 1 (T068zh) |
| user_agent | 0 | — | 0/0 | 3 (preflight reject) |

## Headline architecture metric — self-execute vs delegate

The new MainAgent's decisions across the 46 tasks with trajectories:

- **MainAgent decision-actions:** 153 total — 54 self-execute (business tool), 56 delegate_task, 43 complete.
- **Self vs delegate split (excluding `complete`), n=110:**
  - **self-execute: 54 (49.1%)**
  - **delegate: 56 (50.9%)**
- **Task-level classification:** 6 self-exec-only, 33 delegate-only, 7 mixed.

The split is essentially balanced — the new MainAgent self-executes about as often as it
delegates, a large behavioural change from the old always-delegate MainAgent.

**Self-execute loop fix verified — no re-emerged loops.** Mean trajectory length 3.3 steps,
median 2, max 14 (T128_ticket_assignment, success, 0.92). No task hit the 20-step ceiling.
The two heaviest self-execute sequences — T008_todo_management (10 self-exec actions, 0.76 PASS)
and T128_ticket_assignment (10 self-exec, 0.92 PASS) — both terminated correctly. This is the
multi-step self-execute path that previously looped (T012 was a 20-step loop pre-fix); it now
dedups/terminates cleanly.

## Per-task table

| task_id | category | score | P/F | MainAgent path |
|---|---|---|---|---|
| T001zh_email_triage | mock | 1.00 | PASS | delegate |
| T002_email_triage | mock | 0.87 | PASS | mixed |
| T004_calendar_scheduling | mock | 0.71 | FAIL | delegate |
| T007zh_todo_management | mock | 0.72 | FAIL | mixed |
| T008_todo_management | mock | 0.76 | PASS | self-exec (10) |
| T011zh_expense_report | mock | 0.00 | FAIL | delegate |
| T018_ticket_triage | mock | 0.91 | PASS | delegate |
| T019zh_inventory_check | mock | 0.87 | PASS | delegate |
| T025zh_ambiguous_contact_email | mock | 0.00 | FAIL | self-exec |
| T026_ambiguous_contact_email | mock | 0.00 | FAIL | self-exec |
| T030_cross_service_meeting | mock | 0.83 | PASS | delegate |
| T032_escalation_budget_triage | mock | 0.93 | PASS | delegate |
| T033zh_ops_review_dashboard | mock | 0.97 | PASS | delegate |
| T034_ops_review_dashboard | mock | 0.86 | PASS | delegate |
| T038_incident_postmortem | mock | 0.90 | PASS | delegate |
| T039zh_onboarding_coordinator | mock | 0.86 | PASS | delegate |
| T041zh_scheduled_task_management | mock | 0.98 | PASS | delegate |
| T042_scheduled_task_management | mock | 1.00 | PASS | delegate |
| T074_paper_review_injection | pinbench | 0.97 | PASS | delegate |
| T080_officeqa_bond_yield_change | officeqa | 0.28 | FAIL | mixed |
| T082_officeqa_qoq_esf_change | officeqa | 0.80 | PASS | delegate |
| T083_officeqa_mad_excise_tax | officeqa | 0.95 | PASS | delegate |
| T084_officeqa_geometric_mean_silver | officeqa | 0.79 | PASS | mixed |
| T087_pinbench_market_news_brief | pinbench | 0.80 | PASS | self-exec |
| T094_pinbench_project_alpha_summary | pinbench | 0.71 | FAIL | delegate |
| T098_pinbench_openclaw_facts | pinbench | 0.20 | FAIL | self-exec |
| T107zh_ticket_routing | mock | 0.81 | PASS | mixed |
| T108_ticket_routing | mock | 0.80 | PASS | delegate |
| T117zh_customer_followup | mock | 0.81 | PASS | delegate |
| T118_customer_followup | mock | 0.79 | PASS | delegate |
| T123zh_todo_calendar_conflict | mock | 0.92 | PASS | delegate |
| T128_ticket_assignment | mock | 0.92 | PASS | mixed (10 self) |
| T130_business_trip_planning | mock | 0.79 | PASS | delegate |
| T131zh_order_profit_analysis | mock | 0.76 | PASS | delegate |
| T132_order_profit_analysis | mock | 1.00 | PASS | delegate |
| T144_quarterly_business_insight | mock | 0.72 | FAIL | delegate |
| T149zh_project_progress_report | mock | 0.93 | PASS | delegate |
| T150_project_progress_report | mock | 0.57 | FAIL | delegate |
| T151zh_supply_chain_investigation | mock | 0.96 | PASS | delegate |
| T153zh_market_research_report | mock | 0.97 | PASS | delegate |
| T154_market_research_report | mock | 0.99 | PASS | delegate |
| T155zh_onsite_support_dispatch | mock | 0.95 | PASS | delegate |
| T158_month_end_reconciliation | mock | 0.66 | FAIL | delegate |
| T043zh_service_outage_research | sandbox/web | 0.99 | PASS | delegate |
| T057_deepseek_logo_identification | sandbox/web | 0.20 | FAIL | self-exec |
| T066_finance_bros_gross_profit | sandbox/web | 0.44 | FAIL | mixed |
| T068zh_llama_w8a8_cuda_bug | sandbox/web | ERR | — | needs `--sandbox` |
| C09zh_ai_video_creation | user_agent | ERR | — | preflight reject |
| C10zh_labor_law | user_agent | ERR | — | preflight reject |
| C12zh_ecommerce_operations | user_agent | ERR | — | preflight reject |

## Failures: infra vs agent-quality

### Infra / harness-limitation errors (4 errored tasks — NOT agent quality)

- **C09zh_ai_video_creation, C10zh_labor_law, C12zh_ecommerce_operations** —
  `preflight: aorchestra harness does not support simulated user_agent`. Same coverage gap as
  OpenClaw (which preflight-rejected the same 3). Expected; not forced.
- **T068zh_llama_w8a8_cuda_bug** —
  `--harness aorchestra requires --sandbox when task declares sandbox tools ['Bash']`.
  This sandbox task declares a `Bash` tool and the AO harness refuses to run it without the
  `--sandbox` flag (design doc §4.2), which was not passed. Harness-config limitation, not an
  agent failure. (The other 3 sandbox/web tasks — T043zh, T057, T066 — do NOT declare Bash and
  ran fine in host-mode; T043zh even scored 0.99.)

Infra error rate: 4/50 = 8%, all explained and benign — well under the 30% STOP threshold.

### Agent-quality failures (13 FAILs among the 46 scored)

The notable low scorers are genuine agent mistakes confirmed by inspecting trajectories:

- **T011zh_expense_report (0.00, delegate)** — sub-agent listed transactions then submitted ALL
  13 with the raw total, skipping the required **dedup** (txn_002/txn_003 are identical duplicate
  滴滴 entries) and category assignment. Genuine task failure.
- **T025zh / T026 ambiguous_contact_email (0.00, self-exec)** — "张经理 / Manager Zhang" is an
  **ambiguous** contact; the agent guessed one address and sent without disambiguating / asking
  for clarification. Genuine failure (the disambiguation behaviour the task tests).
- **T098_pinbench_openclaw_facts (0.20), T057_deepseek_logo_identification (0.20),
  T080_officeqa_bond_yield_change (0.28), T066_finance_bros_gross_profit (0.44)** — low-accuracy
  answers on retrieval/computation tasks. Agent quality.

### Grader-connection 0.00 artifacts: NONE recovered

The three 0.00 tasks (T011zh, T025zh, T026) initially looked like the T012-style grader-drop
artifact seen in the 5-task validation, because their **AO raw trajectories** report
`success=True, total_reward=1.0` with a real final answer. They were re-graded individually with
the sonnet judge:

```
grade --trace <jsonl> --task tasks/<id> --config config_concurrency_smoke.yaml
```

All three **re-graded reproducibly to 0.00** (completion 0.00, rule-based). They are therefore
**genuine agent failures**, not transient grader-connection drops — AO's internal
`success=True` is over-optimistic self-assessment that disagrees with claw-eval's rule-based
completion grader (dedup / disambiguation not actually satisfied in the mock-service audit
state). **This run hit zero grader-connection artifacts** (unlike the 5-task validation).

### Re-emerged self-execute loops: NONE

No task approached the 20-step ceiling. The multi-step self-execute path terminates correctly
(T008 and T128 ran 10 self-exec actions each and completed). The loop fix holds across the run.

## Comparison hook

This is the **AO leg**. Sibling run: **OpenClaw — avg 0.657 / 47 tasks** (sonnet judge). AO here:
**avg 0.693 / 50 (errored=0)**, or **0.753 over 46 scored**. These per-leg numbers use the
**sonnet** judge; a **unified-judge 3-way comparison** (baseline / OpenClaw / AO under one judge)
is the next step and is the authoritative cross-harness number. Treat the headline takeaway as the
architecture metric (49% self-execute / 51% delegate, no loops), not the raw score delta, until
the unified judge is applied.

## Readiness for the unified-judge 3-way comparison

- All 50 traces present in `traces/rollout_ao_50task/claude-sonnet-4-5_26-06-26-15-52/`
  (46 scored `.jsonl` + raw, 4 errored).
- No infra instability (8% errored, all explained), no loops, no grader artifacts.
- AO leg is ready to feed into the unified-judge re-grade alongside the baseline and OpenClaw legs.
- Coverage gap note: the 3 user_agent tasks (C09/C10/C12) are unrunnable on **both** OpenClaw and
  AO — they should be excluded from the apples-to-apples comparison set (or scored as 0/errored on
  both legs consistently).
