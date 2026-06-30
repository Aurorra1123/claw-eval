# Infra Smoke Test — New Task Types (post sandbox + web-mock fixes)

**Date:** 2026-06-29
**Goal:** Surface INFRASTRUCTURE bugs (sandbox container startup, web mock routing, bridge
connectivity, port collisions) on three claw-eval tasks that we could not run before two recent
fixes — NOT to chase scores. A low score is fine; a crash / hang / mis-route / container failure is
what we were hunting.

**Fixes under test**
1. AO got a sandbox/container path (`_run_container` + `SANDBOX_TOOLS`, commits e15bc75 / c4d6803).
2. OpenClaw's `web_search` / `web_fetch` now route to the mock service instead of the live internet
   (commit c60b863).

**Harness / runtime:** aorchestra (pi runtime, `CLAWEVAL_AORCHESTRA_RUNTIME=pi`) and openclaw.
**Model:** claude-sonnet-4-5 via deepwisdom newapi. **Judge:** enabled.
**Config:** `config_concurrency_smoke.yaml`. **Sandbox image** (both harnesses): `claw-eval-agent-openclaw:latest`.
**Execution:** serial (`--parallel 1`, `--port-base-offset 0`), absolute `--trace-dir`.

Pi worker build confirmed present:
`/data2/ruanjianhao/AOrchestra/aorchestra/runtime/pi_worker/dist/index.js` (no rebuild needed).

Baseline reference scores: T101 = 0.20, C17 = 0.77, T069 = 0.85.

---

## 3 tasks × 2 harnesses matrix

| task / harness | aorchestra (pi) | openclaw |
|---|---|---|
| **T101_wal_recovery**<br>(empty tools — pure sandbox/container) | **COMPLETED**, score **0.20** (= baseline). Container `claw-agent-T101_wal_recovery-t0-p0` started, sandbox-server reached at host-mapped `http://localhost:32925/exec`, **all 30 Bash calls status 200** w/ real exit codes. Fixtures injected (`test.db` + `test.db-wal` present at start). grader-inject 1/1. env_snapshot ran in-container (`verify_recovery.py` exit 0). Container removed cleanly. `tok=0` (AO/pi path does not surface token counts — cosmetic). | **COMPLETED**, score **0.20** (= baseline). Container started at `http://localhost:8080` (host-net), inject 2/2, grader-inject 1/1, env_snapshot in-container exit 0, container removed. tok=17552 (real). |
| **C17en_devops_sop_design**<br>(Bash + web_search + web_fetch; sandbox + web mock together) | **ERRORED — preflight reject**: `aorchestra harness does not support simulated user_agent`. 0 tokens, instant, no container started. (C17 has `user_agent.enabled: true`.) | **ERRORED — preflight reject**: `openclaw harness does not support simulated user_agent`. 0 tokens, instant. |
| **T069_micron_capex_analysis**<br>(web_search + web_fetch; web mock routing) | **COMPLETED**, score **0.44** (baseline 0.85). Ran AO **host mode** (no `--sandbox`; task has no sandbox tool). `web_real` mock started/stopped on port 9114. **All web_search/web_fetch hit `http://localhost:9114/web/{search,fetch}` status 200** — mock, not live internet. Mock returned empty/404 for Micron URLs (content gap). tok=0 (cosmetic). | **COMPLETED**, score **0.62** (baseline 0.85). Ran with `--sandbox` (OpenClaw CLI gate). `web_real` mock on 9114; container started/removed. **All `tool_dispatch` show `endpoint_url: http://localhost:9114/web/{search,fetch}`, `response_status: 200`** — confirms c60b863 fix: builtin web tools did NOT win the name collision; routing goes bridge→mock. tok=131953 (real). |

---

## Bugs found

### Infra bugs (what we were hunting)
**NONE.** No crash, no hang, no mis-route, no container-startup failure, no port collision across the
six runs. Specifically:
- AO `_run_container` works end-to-end — **no `NotImplementedError`**. The agent received sandbox
  tools (Bash) and they dispatched to the in-container server (status 200, real stdout/stderr/exit).
- Web routing is correct on BOTH harnesses: 100% of web_search/web_fetch calls hit the local mock
  (`localhost:9114`, status 200). Zero live-internet leakage (no getaddrinfo/ENOTFOUND, no
  Cloudflare 403). The c60b863 fix holds — OpenClaw builtin web tools correctly defer to the bridge
  mock tools.
- Containers started and were removed cleanly every time; `paper_eval` container untouched.

### Not-infra observations (expected / out of scope — do NOT confuse with infra bugs)
1. **C17 is un-runnable on either harness (preflight reject).** Both harnesses gate on
   `user_agent.enabled: true` (the simulated-user persona): `aorchestra/harness.py:85` and
   `openclaw.py:163`. This is a *deliberate harness-capability guard*, not a crash — it errors fast
   with 0 tokens and no container. **Consequence:** the "sandbox + web mock together" combination
   could NOT be exercised via C17 on these harnesses. C17 was a poor task choice for this smoke
   matrix. (To test both fixes at once, pick a task that uses Bash + web_* but has NO `user_agent`.)
2. **`web_real` mock has incomplete Micron fixtures (content gap, not a mis-route).** For T069 the
   mock answered every request with status 200, but most Micron search queries returned
   `{"results": [], "total": 0}` and deep financial-statement URLs returned a structured
   `{"status_code": 404, "error": "HTTP 404"}`. Only the bare `https://investors.micron.com` URL has
   a fixture (returns real-looking content), which is why OpenClaw (0.62) edged AO (0.44) — it found
   partial content. The sub-baseline scores are driven by missing mock fixtures + agent quality, NOT
   by routing. `grep -rli micron mock_services/web_real/` → no matches.
3. **AO/pi path reports `tok=0`.** Token counts are not surfaced on the aorchestra pi runtime (both
   AO runs show 0 in/0 out; OpenClaw runs report real tokens). Cosmetic/telemetry, not an infra
   blocker.

---

## Per-harness verdict

### aorchestra (pi runtime)
- **Sandbox startup: SOLID.** `_run_container` is fully implemented and works on the new
  empty-tools/pure-sandbox task (T101). Container start, fixture inject, grader inject, in-container
  Bash dispatch (status 200), and env_snapshot all functioned. No `NotImplementedError`.
- **Web routing: SOLID.** T069 web_search/web_fetch routed to the mock (`localhost:9114`, status 200)
  with zero live-internet leakage.
- **Caveat (not infra):** can't run `user_agent` tasks (preflight reject) — so C17 untested here.
  Telemetry shows tok=0 on pi.

### openclaw
- **Sandbox startup: SOLID.** T101 container started (host-net `:8080`), fixtures/grader injected,
  env_snapshot in-container, clean teardown.
- **Web routing: SOLID — c60b863 confirmed.** T069 `tool_dispatch` events show every web call going
  to `endpoint_url: http://localhost:9114/web/{search,fetch}` with `response_status: 200`. The
  builtin web tools no longer hijack the names; bridge→mock routing wins.
- **Caveat (not infra):** can't run `user_agent` tasks (preflight reject) — so C17 untested here.

**Overall:** The new AO sandbox/container path and the OpenClaw web-mock routing fix are both
infra-solid on these new task types. No infra bug to fix. Two follow-ups are *content/selection*
issues, not infra: (a) C17 needs a non-`user_agent` task to exercise the combined sandbox+web path;
(b) the `web_real` mock needs Micron fixtures if T069 is to score near baseline.

---

## Run log (commands)

All runs: `cd /data2/ruanjianhao/claw-eval && source .venv/bin/activate`, with env
`AORCHESTRA_ROOT=/data2/ruanjianhao/AOrchestra`, deepwisdom newapi creds, model `claude-sonnet-4-5`,
`no_proxy/NO_PROXY=localhost,127.0.0.1`. AO runs add `CLAWEVAL_AORCHESTRA_RUNTIME=pi`.

| # | task | harness | sandbox | trace dir |
|---|---|---|---|---|
| 1 | T101 | aorchestra | `--sandbox` | `traces/infra_smoke_ao_T101` |
| 2 | T101 | openclaw | `--sandbox` | `traces/infra_smoke_oc_T101` |
| 3 | C17 | aorchestra | `--sandbox` | `traces/infra_smoke_ao_C17` (preflight err) |
| 4 | C17 | openclaw | `--sandbox` | `traces/infra_smoke_oc_C17` (preflight err) |
| 5 | T069 | aorchestra | host (no `--sandbox`) | `traces/infra_smoke_ao_T069` |
| 6 | T069 | openclaw | `--sandbox` | `traces/infra_smoke_oc_T069` |

**Cleanup:** all `claw-agent-*` containers `docker rm -f`'d; unrelated `paper_eval` container left running.

**Constraints honored:** no code/config/task.yaml modified; vllm/GPU/port-8002 untouched; serial
execution; judge enabled.
