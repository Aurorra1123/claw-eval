# OpenClaw web_search / web_fetch routed to live internet instead of the mock

## Summary

claw-eval web tasks (e.g. `T043zh_service_outage_research`,
`T066_finance_bros_gross_profit`) declare `web_search` / `web_fetch` tools backed
by a **mock service** with canned fixtures (T043 starts `mock_services/web/server.py`
on port 9113; its `tool_endpoints` route `web_search` → `http://localhost:9113/web/search`).
The bridge plugin is supposed to expose these as the only `web_search`/`web_fetch`
the OpenClaw agent sees, routing every call to the mock.

In the 50-task OpenClaw rollout the agent instead used OpenClaw's **own builtin
`web_search`/`web_fetch`, which hit the LIVE internet** and failed:
`session.jsonl` showed `getaddrinfo ENOTFOUND status.cloudpay.com` (trying to
resolve a fictional mock domain on the real internet) and Cloudflare
`403 Just a moment...`. The bridge traffic log (`bridge_traffic.jsonl`) showed
**0** search/fetch calls — proof the bridge versions were never invoked.

Scores collapsed: T043 = 0.14, T066 = 0.19 (vs cited baselines ~0.93 / ~0.97).

## Root cause: name collision + a name-based deny that can't discriminate

`web_search` and `web_fetch` are the only tool names that are BOTH an OpenClaw
2026.6.x builtin AND a task bridge tool. (`bash`/`read`/`write` don't collide —
tasks don't declare same-named bridge tools.) Two facts combine:

1. **Builtin wins the name-collision dedup.** OpenClaw assembles its core/builtin
   tools first, seeds their names into `existingToolNames`, then resolves plugin
   tools and **silently drops any plugin tool whose normalized name already exists**
   (`dist/openclaw-tools-*.js` `resolveOpenClawPluginToolsForOptions`,
   `dist/tools-*.js` plugin-tool resolution: `plugin tool name conflict … continue`).
   So the bridge's `web_search`/`web_fetch` were never even exposed to the LLM —
   the builtin (live-internet) versions fired.

2. **`tools.deny` is name-based and source-agnostic.** The harness seeds
   `tools.deny` with `_BUILTIN_TOOLS_TO_DENY` (which *does* include `web_fetch`/
   `web_search`). But `_write_tool_policy_config` then ran:
   ```python
   for name in bridge_tool_names:   # includes web_search / web_fetch for web tasks
       deny_set.discard(name)
   ```
   This discard is *correct and necessary*: `tools.deny: ["web_search"]` denies
   ALL tools of that name (builtin AND bridge), so keeping it in deny would kill
   the bridge tool too. But discarding it leaves the **builtin** active, and per
   (1) the builtin wins → live internet → failure.

Net: there is no way to fix this with `tools.deny` (kills both) and no way with
`plugins.deny` (the builtins are *core* tools, not owned by a deniable provider
plugin — denying brave/duckduckgo/exa/etc. only strips a backend, the
`web_search`/`web_fetch` tool stays).

## Fix mechanism chosen: disable the builtin web tools via the core enable flags

The correct lever is OpenClaw's per-tool enable flags. Setting
`tools.web.search.enabled = false` makes `createWebSearchTool` return `null`
(`dist/openclaw-tools-*.js`: `isWebSearchDisabled` → `if (…) return null`), and
`tools.web.fetch.enabled = false` makes `createWebFetchTool` return `null`
(`resolveFetchEnabled` → false). When the builtin returns `null` its name is
**never seeded into the collision set**, so the bridge plugin's same-named tool
**survives the dedup and becomes the sole resolver** for that name — routing the
call to the mock. Per OpenClaw's docs this flag also disables the native
OpenAI/Codex `web_search` path, so it covers our OpenAI-compatible proxy base URL
(`newapi.deepwisdom.ai/v1`) too.

Implemented in `src/claw_eval/harnesses/openclaw.py`,
`OpenClawHarness._write_tool_policy_config`:

- The `discard(name)` loop is kept (bridge names must stay out of the name-based
  deny so the LLM can call them).
- **Added**: when a bridge tool name collides with a builtin web name, seed
  `tools.web.{search,fetch}.enabled = false`:
  ```python
  _WEB_BUILTIN_TO_DISABLE = {"web_search": "search", "web_fetch": "fetch"}
  web_keys_to_disable = {wk for tn, wk in _WEB_BUILTIN_TO_DISABLE.items()
                         if tn in set(bridge_tool_names)}
  if web_keys_to_disable:
      web_block = tools_block.get("web") or {}
      for wk in web_keys_to_disable:
          web_block[wk] = {**(web_block.get(wk) or {}), "enabled": False}
      tools_block["web"] = web_block
  ```

The change is **scoped to the colliding names only**: a task that doesn't declare
a `web_search`/`web_fetch` bridge tool gets no `tools.web` block at all, and its
builtin `web_search`/`web_fetch` remain in `tools.deny` (so non-web tasks still
have zero live-internet exposure). Both execution paths preserve the seeded block:
`_openclaw_native._build_openclaw_temp_config` (and the container path that calls
it) read the seeded `openclaw.json` and only mutate `models`/`agents`, leaving
`tools.web` intact.

No task.yaml files, no bridge generator tool names, and no other harness were
touched.

## Before / after evidence

Verification rerun (T043 + T066, OpenClaw, sandbox, judge enabled, trials=1):

| Task   | Score before | Score after | Bridge web calls before → after | Bridge status (after) |
|--------|--------------|-------------|----------------------------------|-----------------------|
| T043zh | 0.1418 FAIL  | **0.97 PASS** | 0 → 12 (5 search + 7 fetch)      | all 200, `localhost:9113/web/*` |
| T066   | 0.192 FAIL   | **0.44 FAIL** | 0 → 6 (5 search + 1 fetch)       | all 200, `localhost:9164/web/*` |

- **Before** (`traces/rollout_openclaw_50task/.../{T043,T066}_…_raw/raw/`):
  `bridge_traffic.jsonl` had **0 lines**; `session.jsonl` had the live-internet
  errors (`getaddrinfo ENOTFOUND status.cloudpay.com`, Cloudflare `Just a moment`).
- **After** (`traces/web_tool_fix_verify/claude-sonnet-4-5_26-06-29-19-07/`):
  every bridge web call is HTTP 200 against the mock service ports; no
  `ENOTFOUND` / live Cloudflare 403. (The lone `403` in T066's session is *mock
  fixture content* — the canned `{"status_code":403,"url":"…dutchbros.com…"}`
  payload the agent must handle — delivered over a bridge call with HTTP
  `status:200`, not a live-internet failure.)
- The seeded `openclaw.json` for the verify run contained
  `tools.web = {"fetch":{"enabled":false},"search":{"enabled":false}}` and an
  empty web entry in `tools.deny`, exactly as designed.

T043 reaches parity with baseline (0.97). T066 rose substantially but is still
below baseline; the residual gap is the agent's reasoning quality, not tooling —
the judge breakdown is R=1.00 (retrieval correct, i.e. the mock was reached) with
C=0.30 (content/answer), confirming the bridge now works and the remainder is a
modeling/answer issue, not the bug.

## Implication for the 50-task rollout

Affected tasks (declare `web_search`/`web_fetch` AND showed 0 bridge web calls
in the rollout): **T043zh, T066, T068zh**. (T057 also declares a web tool but its
low score is the separate vision/multimodal issue — its rollout bridge traffic
shows `caption_describe_image`, not web — so it is not affected by this bug.)

Rollout average before: **0.6572** (50 tasks, 3 errored).

Per-task deltas measured: T043 **+0.828**, T066 **+0.248** (sum +1.076 over 50
tasks ⇒ **+0.0215** to the average).

Including T068zh (rerun not performed; it also had 0 bridge web calls so it is
genuinely undervalued):

| Scenario for T068zh        | Total Δ | Avg shift | Projected 50-task avg |
|----------------------------|---------|-----------|------------------------|
| unchanged (0.64)           | +1.076  | +0.0215   | ~**0.679**             |
| recovers to ~0.90 (T043-like) | +1.336 | +0.0267 | ~**0.684**             |

So a full rerun would lift OpenClaw's 50-task average by roughly **+0.022 to
+0.027** (0.657 → ~0.679–0.684).

## Is a full 50-task rerun warranted?

**No full rerun needed for this bug.** The fix is local to web tasks and
deterministic in scope; only T043/T066/T068zh are affected, and T043/T066 are
already re-measured. The cleanest path is to **patch the 3 web-task scores into
the existing 50-task results** (T043→0.97, T066→0.44, T068zh→rerun once), which
yields the projected ~0.68 average without re-spending budget on the other 47
unaffected tasks. The only outstanding rerun is the single task **T068zh** to
turn its conservative estimate into a measured number.
