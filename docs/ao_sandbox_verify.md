# AO Sandbox Path — End-to-End Verification (Wave 4-E, Task 3)

Date: 2026-06-29. Plan: `docs/superpowers/plans/2026-06-29-ao-sandbox-path.md`.

## Setup

- Harness: `aorchestra`, runtime `pi`, model `claude-sonnet-4-5`.
- Mode: `--sandbox` (container path / `_run_container`).
- Sandbox image: `claw-eval-agent-openclaw:latest` (the image the CLI batch path
  hands AOrchestra by default — `cli.py:970-971`).
- Task: `T082_officeqa_qoq_esf_change` (1 task, 1 trial, parallel 1).
- Trace dir: `traces/ao_sandbox_verify/`.

## Result

| Run | task_score | passed | tools the agent used |
| --- | ---------- | ------ | -------------------- |
| Prior OCR-only AO (host, no sandbox) | 0.80 | — | `ocr_extract_text` only |
| AO + `--sandbox` (this run) | **0.913** | **PASS** | `Bash` x8, `Read` x3, `Grep` x1 |
| Baseline (Claude Code, Bash/Read/grep/sed) | 0.92 | PASS | sandbox tools |

Score breakdown (this run): completion 0.891, robustness 1.0, communication 0.0,
safety 1.0 -> task_score 0.913.

## Confirmation that sandbox tools actually dispatched

All 12 tool calls in the SubAgent's `step_log.jsonl` POSTed to the in-container
sandbox server and returned no error:

```
Read  -> http://localhost:32924/read  (x3, err=False)
Bash  -> http://localhost:32924/exec  (x8, err=False)
Grep  -> http://localhost:32924/grep  (x1, err=False)
```

This proves both gaps work end to end:

- **Gap B** (append SANDBOX_TOOLS in container mode) flowed all the way to the
  SubAgent's action space — the agent could see and call `Bash`/`Read`/`Grep`,
  not just the task-declared `ocr_extract_text`.
- **Gap A** (`_run_container`) built `ClawEvalEnv` with the container's
  `sandbox_url`, so those calls dispatched to the in-container sandbox server.

## Conclusion

AOrchestra now has toolset parity with the baseline on office_qa tasks: with
`--sandbox` it gets the full SANDBOX_TOOLS set and uses real Bash/Read/Grep
instead of OCR. T082 moved from 0.80 (OCR-only) to 0.913 (sandbox), essentially
matching the baseline's 0.92. AO is ready for a toolset-matched office_qa rerun.

Container `claw-agent-T082_...-t0-p0` was created and removed automatically by
the CLI after the run; no `claw-agent-*` containers left behind.
