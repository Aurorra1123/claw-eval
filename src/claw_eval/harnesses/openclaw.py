"""OpenClawHarness — drive a task rollout through the OpenClaw CLI agent.

Phase 3 §3.4 / §3.4a / §3.6 / §6.5 — see ``docs/harness_design.md``.

The harness orchestrates four moving parts in a fixed order:

1. **Prepare work_dir** under ``trace_dir``: copy ``task.sandbox_files``
   (or ``environment.fixtures``) so the OpenClaw subprocess can read them.

2. **Generate + install a bridge plugin** (``_openclaw_bridge``) so the LLM's
   tool calls hit claw-eval's mock services with real HTTP semantics. The
   plugin's traffic log is the source of truth for ``ToolDispatch``
   ``response_status`` / ``response_body``.

3. **Run OpenClaw subprocess** (``_openclaw_native.run``) inside the bridge's
   isolated state dir. The bridge logs every fetch into a JSONL file the
   translator reads back.

4. **Translate session + bridge log** (``_trace_adapter.translate_openclaw``)
   into a claw-eval JSONL trace + collect ``services_ctx`` audit data.

5. **Inject grader files + run snapshot** (``_snapshot``) on the host workdir
   — strictly AFTER the OpenClaw process exits so the agent never sees the
   grader-only files (verify scripts with answers).

The contract: ``HarnessResult.trace_path`` is byte-compatible with the
``ClawEvalHarness`` output schema. Graders consume it unchanged.

Cross-task isolation note: the bridge plugin lives under an isolated
``OPENCLAW_STATE_DIR`` set to ``<case_dir>/raw/openclaw_state`` — the same
path the native runner would create. Bridge installs first, native runner
reuses the dir, both processes see the same plugin set. ``bridge.cleanup()``
rm-rf's the state dir at the end; we run it AFTER ``translate_openclaw``
since the bridge log lives under the case dir we're about to delete.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from . import _openclaw_bridge, _openclaw_container, _openclaw_native
from ._snapshot import collect_workdir_snapshot, inject_grader_files_host
from ._trace_adapter import translate_openclaw
from .base import HarnessResult

if TYPE_CHECKING:
    from ..config import Config
    from ..models.task import TaskDefinition
    from ..runner.sandbox_runner import ContainerHandle
    from ..runner.services import ServiceManager
    from ..runner.user_agent import UserAgent


_log = logging.getLogger(__name__)


def _resolve_bridge_network(
    env: "dict[str, str]",
    *,
    sandbox_port: int,
    host_sandbox_url: str,
) -> "tuple[str | None, str]":
    """Decide bridge-vs-host networking for the openclaw container (macOS compat).

    Returns ``(host_gateway, sandbox_url_for_plugin)``.

    Opt-in via ``CLAWEVAL_SANDBOX_NET=bridge`` (default/empty = host mode):

    - **host mode** (Linux, default): ``(None, host_sandbox_url)`` — no URL
      rewrite, the bridge plugin reaches both the sandbox server and host mock
      services via the shared host network. Unchanged behaviour.
    - **bridge mode** (macOS Docker Desktop): ``("host.docker.internal",
      "http://localhost:<sandbox_port>")``. Two distinct fixes:
        * ``host_gateway`` rewrites mock-service URLs so the bridged (non-host)
          container can still reach host mocks.
        * the plugin's SANDBOX_TOOLS must target the IN-CONTAINER sandbox port
          (``localhost:<sandbox_port>``), NOT ``host_sandbox_url`` which is the
          HOST-mapped port — only valid host-side (probe/snapshot), wrong inside
          the container. Getting this wrong makes Bash/Read/Write 404.
    """
    bridge_mode = str(env.get("CLAWEVAL_SANDBOX_NET", "")).strip().lower() == "bridge"
    if not bridge_mode:
        return None, host_sandbox_url
    return "host.docker.internal", f"http://localhost:{sandbox_port}"



# OpenClaw 2026.6.x built-in tools that we don't want the LLM to use during a
# claw-eval task — they let the model bypass the bridge plugin (which is the
# whole point of evaluating tool use). Derived empirically from the
# ``[agents/tool-policy]`` diagnostic that fires when ``tools.profile`` is set,
# plus a manual sweep of the OpenClaw 2026.6.8 ``openclaw plugins list``.
#
# Keep ``session_status`` and ``multi_tool_use.parallel`` *out* of this list —
# they're read-only / convenience tools the agent loop relies on for basic
# scaffolding (status display, parallel tool batching). Removing them would
# trip the embedded-runner "No callable tools remain" guard before the LLM
# even gets to call the bridge tool.
# Minimal scaffolding tools kept callable even under the strict bridge
# allowlist: ``session_status`` (read-only diagnostic) and
# ``multi_tool_use.parallel`` (provider-level fan-out convenience). Everything
# else the agent sees must be a bridge plugin tool — see
# ``_write_tool_policy_config``. (Replaces the old ``_BUILTIN_TOOLS_TO_DENY``
# blacklist: an allowlist excludes every builtin by construction, so we no
# longer enumerate them, and same-named builtins can no longer hijack a bridge
# tool.)
_BRIDGE_SCAFFOLDING_TOOLS = [
    "session_status",
    "multi_tool_use.parallel",
]

# Bridge tool names that COLLIDE with an OpenClaw *managed* tool (not just a
# plugin tool): the managed implementation wins at dispatch time even when an
# allowlist makes the same-named bridge tool visible (stderr proof: "web_search
# is disabled or no provider is available" / web_fetch hitting the real domain).
# For these we must turn the managed tool OFF via its dedicated config switch so
# the bridge plugin's same-named tool is the only one that resolves. Maps the
# colliding bridge tool name -> the ``tools.web.*`` switch path to disable.
_MANAGED_WEB_TOOL_SWITCHES = {
    "web_search": ("search",),
    "web_fetch": ("fetch",),
}


class OpenClawHarness:
    """Drive a task using the OpenClaw external CLI agent.

    Two execution modes:

    * **container** (production, §3.7) — ``sandbox_handle`` provided. OpenClaw
      runs inside the sandbox container via ``docker exec``; SANDBOX_TOOLS
      (Bash/Read/Write/...) bridge to the container's sandbox server while
      mock-service tools fetch host endpoints via ``--network host``.
    * **host smoke** (Wave 3-D legacy) — ``sandbox_handle is None``. OpenClaw
      runs on host. Only HTTP mock-service tools are bridgeable; tasks
      declaring ``Bash`` etc are rejected by preflight in this mode.

    ``supported_features`` includes ``sandbox_tools`` because the container
    mode bridges them to the sandbox server. ``user_agent`` and ``compact``
    are still omitted — they rely on claw-eval loop-internal hooks that
    don't translate to the OpenClaw one-shot model.
    """

    name = "openclaw"
    supported_features = frozenset({"http_services", "sandbox_tools"})

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def preflight(self, task: "TaskDefinition") -> list[str]:
        """Reject tasks whose semantics OpenClaw can't honour.

        Strict rejections (all modes):

        - ``user_agent.enabled``: OpenClaw is a one-shot CLI; it can't accept
          mid-run injected user replies.

        Soft rejections (handled by container mode, not host smoke):

        - ``task.tools`` containing SANDBOX_TOOL_NAMES (Bash / Read / ...):
          accepted by preflight (container mode handles them via the bridge
          to the sandbox server). Host smoke mode catches this later when
          generating the bridge plugin without a ``sandbox_url`` and raises
          there with a clearer message.

        - ``services`` / ``tool_endpoints``: NOT rejected. The bridge plugin
          (§3.4a) translates them into native OpenClaw tools that fetch the
          mock services directly.
        """
        errs: list[str] = []
        ua = getattr(task, "user_agent", None)
        if ua is not None and getattr(ua, "enabled", False):
            errs.append("openclaw harness does not support simulated user_agent")
        return errs

    # ------------------------------------------------------------------
    # Run — dispatcher
    # ------------------------------------------------------------------

    def run(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        cfg: "Config",
        sandbox_handle: "ContainerHandle | None",
        user_agent: "UserAgent | None",
        services_ctx: "ServiceManager | None",
        sandbox_tools: bool = False,
    ) -> HarnessResult:
        """Dispatch to container or host smoke path.

        * ``sandbox_handle`` provided -> ``_run_container`` (production, §3.7).
        * ``sandbox_handle is None`` -> ``_run_host_smoke`` (Wave 3-D, smoke
          test only — not for production evaluation, see design doc §7).
        """
        if sandbox_handle is not None:
            return self._run_container(
                task,
                trace_dir=trace_dir,
                run_id=run_id,
                cfg=cfg,
                sandbox_handle=sandbox_handle,
                services_ctx=services_ctx,
            )
        return self._run_host_smoke(
            task,
            trace_dir=trace_dir,
            run_id=run_id,
            cfg=cfg,
            services_ctx=services_ctx,
        )

    # ------------------------------------------------------------------
    # Container path (production — §3.7)
    # ------------------------------------------------------------------

    def _run_container(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        cfg: "Config",
        sandbox_handle: "ContainerHandle",
        services_ctx: "ServiceManager | None",
    ) -> HarnessResult:
        """Production form: OpenClaw + bridge + sandbox server in container,
        mock service + audit on host (§3.7).

        Lifecycle (§3.7 step ordering):

        1. Bridge plugin: generate sources on host, install via docker exec
           inside the container's isolated OpenClaw state dir.
        2. inject_files (fixtures) via sandbox server.
        3. ``docker exec openclaw agent ...`` — sandbox-tool calls route to
           the container's sandbox server; mock-service tool calls route
           through ``--network host`` to host mock services.
        4. inject_grader_files via sandbox server (AFTER agent exits).
        5. env_snapshot via sandbox server (BEFORE container stop, which
           the caller handles).
        6. audit_data from mock services (the host process — still alive).
        7. Translate session.jsonl + bridge log into a claw-eval trace.

        The caller (CLI / e2e test) owns ``sandbox_handle.container``'s
        lifecycle — we never stop the container from inside the harness.
        """
        from ..cli import _collect_env_snapshot
        from ..runner.sandbox_runner import SandboxRunner

        trace_dir = Path(trace_dir)
        trace_dir.mkdir(parents=True, exist_ok=True)
        task_dir = self._task_dir(task)

        # Case scratch dir + raw subdir. The host harness mounts case_dir at
        # the same path inside the container (see CLI / e2e test) so
        # everything under ``raw_dir`` is visible on both sides.
        case_dir = trace_dir / f"{task.task_id}_{run_id}_raw"
        case_dir.mkdir(parents=True, exist_ok=True)
        raw_dir = case_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        sandbox_url = sandbox_handle.sandbox_url

        # Bridge network mode (macOS compat, opt-in via CLAWEVAL_SANDBOX_NET=bridge).
        # See _resolve_bridge_network for the routing rationale. host_sandbox_url
        # (sandbox_handle.sandbox_url) stays valid host-side for probe/snapshot.
        host_gateway, bridge_sandbox_url = _resolve_bridge_network(
            os.environ,
            sandbox_port=cfg.sandbox.sandbox_port,
            host_sandbox_url=sandbox_url,
        )

        # ---- 1. Bridge plugin: generate on host, install in container ----
        bridge = _openclaw_bridge.generate_and_install(
            task=task,
            case_dir=raw_dir,
            services_ctx=services_ctx,
            run_id=run_id,
            sandbox_url=bridge_sandbox_url,
            host_gateway=host_gateway,
            container=sandbox_handle.container,
        )

        # ---- 1b. Seed tools.allow (same as host smoke path) ----
        bridge_tool_names = _openclaw_bridge.generator._bridgeable_tools(task)  # type: ignore[attr-defined]
        config_path = raw_dir / "openclaw.json"
        self._write_tool_policy_config(
            config_path=config_path,
            bridge_tool_names=bridge_tool_names,
            bridge_plugin_id=bridge.plugin_id,
        )

        try:
            # ---- 2. Inject fixtures via sandbox server ----
            SandboxRunner.inject_files(
                sandbox_handle, task, task_dir=str(task_dir)
            )

            # ---- 3. Run OpenClaw inside the container ----
            #
            # The CLAWEVAL_BRIDGE_LOG env was set on container start
            # (volumes + extra_env) so the plugin can append to the
            # host-visible log file inside the container.
            raw = _openclaw_container.run_in_container(
                prompt=task.prompt.text,
                container=sandbox_handle.container,
                work_dir_host="/workspace",
                case_dir_host=str(case_dir),
                timeout_s=float(task.environment.timeout_seconds),
                api_provider={
                    "baseUrl": cfg.model.base_url,
                    "model": cfg.model.model_id,
                    "apiKey": cfg.model.api_key,
                    "provider_type": "openai",
                },
                extra_plugins=[bridge.plugin_id] if bridge.plugin_id else [],
                seeded_config_path=str(config_path),
            )

            # ---- 4. Inject grader files (AFTER agent exit) ----
            SandboxRunner.inject_grader_files(
                sandbox_handle, task, task_dir=str(task_dir)
            )

            # ---- 5. env_snapshot via sandbox server (BEFORE container stop) ----
            env_snapshot = _collect_env_snapshot(sandbox_url, task)

            # ---- 6. Collect audit from host mock services ----
            audit_data = self._collect_audit(task, services_ctx)

            # ---- 7. Translate trace ----
            trace_blob = raw.get("trace") or {}
            trace_path = translate_openclaw(
                execution_trace=trace_blob.get("executionTrace") or [],
                usage_total=trace_blob.get("usageTotal") or {},
                llm_meta=trace_blob.get("llm") or {},
                bridge_log_path=bridge.traffic_log_path,
                audit_data=audit_data,
                task=task,
                run_id=run_id,
                trace_dir=trace_dir,
                duration_ms=int(raw.get("durationMs") or 0),
                status=str(raw.get("status") or "ok"),
            )

            raw_dir_path = (
                Path(trace_blob.get("rawDir"))
                if isinstance(trace_blob.get("rawDir"), str) and trace_blob.get("rawDir")
                else raw_dir
            )
            return HarnessResult(
                trace_path=trace_path,
                env_snapshot=env_snapshot,
                audit_data=audit_data,
                raw_dir=raw_dir_path,
            )
        finally:
            bridge.cleanup()

    # ------------------------------------------------------------------
    # Host smoke path (Wave 3-D — preserved unchanged)
    # ------------------------------------------------------------------

    def _run_host_smoke(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        cfg: "Config",
        services_ctx: "ServiceManager | None",
    ) -> HarnessResult:
        """Host smoke test path - Wave 3-D only.

        Does not provide process isolation. Does NOT support tasks that
        declare SANDBOX_TOOL_NAMES (no place to bridge them on host). The
        bridge will raise ``SchemaTranslationError`` if it encounters one.

        Preserved verbatim from Wave 3-D so the e2e smoke test in
        ``tests/test_openclaw_e2e.py`` keeps passing as a regression guard.
        See design doc §7 — host mode is forbidden for production
        evaluation.
        """
        return self._run_host_smoke_impl(
            task,
            trace_dir=trace_dir,
            run_id=run_id,
            cfg=cfg,
            services_ctx=services_ctx,
        )

    # ------------------------------------------------------------------
    # Internals — task_dir helper + host smoke impl + shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _task_dir(task: "TaskDefinition") -> Path:
        return (
            Path(task.task_file).parent
            if getattr(task, "task_file", None)
            else Path.cwd()
        )

    def _run_host_smoke_impl(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        cfg: "Config",
        services_ctx: "ServiceManager | None",
    ) -> HarnessResult:
        trace_dir = Path(trace_dir)
        trace_dir.mkdir(parents=True, exist_ok=True)
        task_dir = self._task_dir(task)

        # ---- 1. Prepare work_dir (agent's view of the world) ----
        work_dir = self._prepare_workdir(
            task,
            trace_dir=trace_dir,
            run_id=run_id,
            task_dir=task_dir,
        )

        # ---- 2. Case scratch dir (raw + bridge + plugin live here) ----
        case_dir = trace_dir / f"{task.task_id}_{run_id}_raw"
        case_dir.mkdir(parents=True, exist_ok=True)

        # The native runner builds its OPENCLAW_STATE_DIR / HOME under
        # ``<case_dir>/raw`` (see _openclaw_native.run, lines ~973-980). Anchor
        # the bridge to that same ``<case_dir>/raw`` directory so the plugin
        # the bridge installs is the SAME state dir the openclaw subprocess
        # reads from. Without this anchoring, bridge and native runner would
        # each carve their own state dirs and the LLM would see zero tools.
        raw_dir = case_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        # ---- 3. Generate + install the bridge plugin ----
        bridge = _openclaw_bridge.generate_and_install(
            task=task,
            case_dir=raw_dir,
            services_ctx=services_ctx,
            run_id=run_id,
        )

        # ---- 3b. Seed tools.allow so the LLM sees only the bridge tools ----
        #
        # Without this, the OpenClaw agent surfaces ~40 built-in tools (read,
        # write, exec, process, pdf, browser, ...) to the model. The §6.5
        # contract ("the model's only visible tool set during a task ==
        # bridge plugin's tools") requires us to disable those built-ins.
        #
        # Mechanism: write a partial ``openclaw.json`` at the path the native
        # runner will use as ``OPENCLAW_CONFIG_PATH`` (raw_dir/openclaw.json).
        # ``_openclaw_native._build_openclaw_temp_config`` reads this file
        # first, then adds the ``models`` / ``agents`` keys without clobbering
        # any other top-level fields. So ``tools.allow`` survives the merge.
        #
        # An ALLOW allowlist (not a deny blacklist): the deny approach could
        # not isolate a bridge tool whose name collides with a builtin
        # (web_search / web_fetch) — OpenClaw deny matches by bare name, so
        # un-denying the collision revived the builtin, which hijacked the call.
        # An allowlist names the bridge tool directly. (OpenClaw only errors
        # "No callable tools remain" when NOTHING in the allow list matches a
        # registered tool; the bridge plugin registers these names by the time
        # the policy resolves, so they're callable — verified on 2026.6.9.)
        bridge_tool_names = [ep.tool_name for ep in (task.tool_endpoints or [])]
        config_path = raw_dir / "openclaw.json"
        self._write_tool_policy_config(
            config_path=config_path,
            bridge_tool_names=bridge_tool_names,
            bridge_plugin_id=bridge.plugin_id,
        )

        try:
            # The bridge recorder reads CLAWEVAL_BRIDGE_LOG from its node
            # subprocess env. _openclaw_native.run does ``env =
            # os.environ.copy()`` so we inject into os.environ for the
            # duration of the call. Save/restore to avoid leaking into the
            # rest of the test session.
            prev_log_env = os.environ.get("CLAWEVAL_BRIDGE_LOG")
            prev_cfg_env = os.environ.get("OPENCLAW_CONFIG_PATH")
            if bridge.traffic_log_path is not None:
                os.environ["CLAWEVAL_BRIDGE_LOG"] = str(bridge.traffic_log_path)
            # Point the native runner at our seeded config so the tools.deny
            # entry sticks across the ``_build_openclaw_temp_config`` merge.
            os.environ["OPENCLAW_CONFIG_PATH"] = str(config_path)

            # ---- 4. OpenClaw subprocess ----
            try:
                raw = _openclaw_native.run(
                    prompt=task.prompt.text,
                    work_dir=str(work_dir),
                    sandbox_dir=str(case_dir),
                    timeout_s=float(task.environment.timeout_seconds),
                    api_provider={
                        "baseUrl": cfg.model.base_url,
                        "model": cfg.model.model_id,
                        "apiKey": cfg.model.api_key,
                        "provider_type": "openai",
                    },
                    extra_plugins=[bridge.plugin_id] if bridge.plugin_id else [],
                )
            finally:
                if prev_log_env is None:
                    os.environ.pop("CLAWEVAL_BRIDGE_LOG", None)
                else:
                    os.environ["CLAWEVAL_BRIDGE_LOG"] = prev_log_env
                if prev_cfg_env is None:
                    os.environ.pop("OPENCLAW_CONFIG_PATH", None)
                else:
                    os.environ["OPENCLAW_CONFIG_PATH"] = prev_cfg_env

            # ---- 5. Collect audit BEFORE bridge cleanup wipes anything ----
            audit_data = self._collect_audit(task, services_ctx)

            # ---- 6. Translate the run into a claw-eval trace JSONL ----
            trace_blob = raw.get("trace") or {}
            trace_path = translate_openclaw(
                execution_trace=trace_blob.get("executionTrace") or [],
                usage_total=trace_blob.get("usageTotal") or {},
                llm_meta=trace_blob.get("llm") or {},
                bridge_log_path=bridge.traffic_log_path,
                audit_data=audit_data,
                task=task,
                run_id=run_id,
                trace_dir=trace_dir,
                duration_ms=int(raw.get("durationMs") or 0),
                status=str(raw.get("status") or "ok"),
            )

            # ---- 7. inject grader files + snapshot (§3.6) ----
            #
            # CRITICAL ordering: grader files copy in AFTER the openclaw
            # subprocess has returned (which it has — we're past
            # _openclaw_native.run). The agent can't read these files
            # because its process is already gone.
            inject_grader_files_host(task, work_dir, task_dir=task_dir)
            env_snapshot = collect_workdir_snapshot(work_dir, task, task_dir=task_dir)

            raw_dir_path = (
                Path(trace_blob.get("rawDir"))
                if isinstance(trace_blob.get("rawDir"), str) and trace_blob.get("rawDir")
                else raw_dir
            )
            return HarnessResult(
                trace_path=trace_path,
                env_snapshot=env_snapshot,
                audit_data=audit_data,
                raw_dir=raw_dir_path,
            )
        finally:
            # Bridge cleanup wipes the plugin dir + isolated state dirs but
            # leaves the surrounding case_dir (which still holds the native
            # runner's raw outputs: session.jsonl, stdout.txt, preflight, ...)
            # untouched. This matches the §3.4a contract: "rm -rf
            # case_state case_home plugin_dir", nothing else.
            bridge.cleanup()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prepare_workdir(
        self,
        task: "TaskDefinition",
        *,
        trace_dir: Path,
        run_id: str,
        task_dir: Path,
    ) -> Path:
        """Materialise a per-run workdir and copy task fixtures into it.

        Priority mirrors ``SandboxRunner.inject_files``:
        1. ``task.sandbox_files`` if set
        2. otherwise ``task.environment.fixtures``

        Files preserve their relative paths under ``work_dir`` so the agent's
        relative-path references inside ``task.prompt.text`` still resolve.
        Missing sources are logged but do not abort — the agent may still
        complete the task without optional fixtures.
        """
        work_dir = trace_dir / f"{task.task_id}_{run_id}_workdir"
        work_dir.mkdir(parents=True, exist_ok=True)

        file_list: list[str] = list(task.sandbox_files or [])
        if not file_list:
            file_list = list(getattr(task.environment, "fixtures", None) or [])

        copied = 0
        for rel in file_list:
            src = task_dir / rel
            dst = work_dir / rel
            if not src.exists():
                _log.warning("sandbox fixture not found, skipping: %s", src)
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except OSError as exc:
                _log.warning("fixture copy failed (%s -> %s): %s", src, dst, exc)
        if file_list:
            _log.info(
                "openclaw harness: %d/%d fixtures copied into %s",
                copied, len(file_list), work_dir,
            )
        return work_dir

    @staticmethod
    def _write_tool_policy_config(
        *,
        config_path: Path,
        bridge_tool_names: list[str],
        bridge_plugin_id: str | None,
    ) -> None:
        """Seed ``openclaw.json`` with ``tools.allow`` + ``plugins.allow``.

        Written BEFORE the OpenClaw subprocess starts. The native runner's
        ``_build_openclaw_temp_config`` reads any existing config first and
        only modifies ``models`` / ``agents``, so the ``tools`` / ``plugins``
        keys we seed here survive the merge unchanged.

        §6.5 contract: the model's only visible tools during a task ARE the
        bridge plugin's tools. We enforce this with an ALLOW allowlist —
        ``bridge_tool_names`` + ``_BRIDGE_SCAFFOLDING_TOOLS`` (session_status,
        multi_tool_use.parallel) — so every builtin is excluded by construction.

        This replaces an earlier ``tools.deny`` blacklist that had to ``discard``
        a bridge tool colliding with a builtin name (web_search / web_fetch);
        because OpenClaw 2026.6.x deny matches by bare tool NAME with no source
        dimension, un-denying the collision revived the BUILTIN, which hijacked
        the call (the bridge mock saw nothing). An allowlist has no such
        collision: it names the bridge tool, and the bridge plugin's tool is the
        only one that resolves.
        """
        import json as _json

        existing: dict = {}
        if config_path.exists():
            try:
                existing = _json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, _json.JSONDecodeError):
                existing = {}

        tools_block = existing.get("tools") if isinstance(existing.get("tools"), dict) else {}
        existing_allow = tools_block.get("allow") if isinstance(tools_block.get("allow"), list) else []
        # ALLOW allowlist (not deny blacklist). The §6.5 contract requires the
        # model's only visible tools to BE the bridge plugin's tools. We list
        # exactly those (+ minimal scaffolding) so EVERY builtin is excluded —
        # including one that shares a name with a bridge tool (web_search /
        # web_fetch). OpenClaw 2026.6.x tools.deny matches by normalized bare
        # tool NAME with no source dimension, so the old deny+discard approach
        # could not un-deny a colliding bridge tool without also reviving the
        # builtin (which then hijacked the call). An allowlist names the bridge
        # tool directly, so the bridge plugin's web_search is the only one that
        # resolves. (allow only errors "No callable tools remain" when NOTHING
        # in the list matches a registered tool — the bridge plugin registers
        # these names, so they resolve.)
        allow_set = (
            set(existing_allow)
            | set(bridge_tool_names)
            | set(_BRIDGE_SCAFFOLDING_TOOLS)
        )
        tools_block["allow"] = sorted(allow_set)
        # Drop any stale deny we (or a prior run) may have seeded — under an
        # allowlist it is redundant, and a deny entry for a bridge tool would
        # re-introduce the collision we just eliminated.
        tools_block.pop("deny", None)

        # Disable any OpenClaw *managed* web tool whose name a bridge tool
        # claims (web_search / web_fetch). An allowlist alone does not stop the
        # managed implementation from winning dispatch; its dedicated switch
        # (tools.web.<search|fetch>.enabled=false) does, leaving the bridge
        # plugin's same-named tool as the only one that resolves. Only touch
        # tools.web when there is an actual collision (no gratuitous config).
        web_disables = [
            switch
            for name in bridge_tool_names
            for switch in _MANAGED_WEB_TOOL_SWITCHES.get(name, ())
        ]
        if web_disables:
            web_block = tools_block.get("web") if isinstance(tools_block.get("web"), dict) else {}
            for switch in web_disables:
                sub = web_block.get(switch) if isinstance(web_block.get(switch), dict) else {}
                sub["enabled"] = False
                web_block[switch] = sub
            tools_block["web"] = web_block

        existing["tools"] = tools_block

        # Pre-register the bridge plugin under ``plugins.allow`` so the
        # OpenClaw policy filter doesn't print a warning every run. Idempotent
        # with the bridge install (which also writes plugins.entries).
        if bridge_plugin_id:
            plugins_block = (
                existing.get("plugins") if isinstance(existing.get("plugins"), dict) else {}
            )
            allow = (
                plugins_block.get("allow")
                if isinstance(plugins_block.get("allow"), list)
                else []
            )
            if bridge_plugin_id not in allow:
                allow = list(allow) + [bridge_plugin_id]
            plugins_block["allow"] = allow
            existing["plugins"] = plugins_block

        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            _json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _collect_audit(
        task: "TaskDefinition",
        services_ctx: "ServiceManager | None",
    ) -> dict[str, dict]:
        """Pull ``/audit`` from every mock service declared by ``task``.

        We don't depend on ``ServiceManager`` exposing a ``collect_audit``
        helper (it doesn't, today) — instead we replicate the loop.py:530-545
        pattern: derive ``audit_url`` from each service's reset endpoint by
        swapping ``/reset`` -> ``/audit``. ``services_ctx`` is accepted only
        to keep the signature symmetric with claweval; we don't use it.
        """
        audit: dict[str, dict] = {}
        if not getattr(task, "services", None):
            return audit
        # Best-effort: import httpx lazily so harness import doesn't pull it
        # at module-import time (matches runner/loop.py:531).
        import httpx

        for svc in task.services:
            if not getattr(svc, "reset_endpoint", None):
                continue
            audit_url = svc.reset_endpoint.rsplit("/reset", 1)[0] + "/audit"
            try:
                resp = httpx.get(audit_url, timeout=5)
                audit[svc.name] = resp.json() if resp.status_code == 200 else {
                    "error": f"audit fetch failed: HTTP {resp.status_code}"
                }
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning("audit fetch failed for service %s: %s", svc.name, exc)
                audit[svc.name] = {"error": str(exc)}
        return audit
