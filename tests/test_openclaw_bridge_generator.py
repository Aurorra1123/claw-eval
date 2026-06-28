"""Wave 2 unit tests for the OpenClaw bridge plugin generator (§6.3).

We deliberately do NOT exercise the install chain (``npm install`` /
``openclaw plugins ...``) — that lands in Wave 3 OpenClaw e2e. These tests
cover the *pure* slice: TS source rendering, JSON Schema -> TypeBox
translation, and BridgeHandle lifecycle.

The bus error in the harness's pytest binary doesn't affect direct execution:
each test function is also runnable as ``python tests/test_openclaw_bridge_generator.py``.
"""

from __future__ import annotations

import sys
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "tasks"

# In some CI setups conftest.py adds src/ to sys.path; we duplicate the line
# here so the file also runs as ``python tests/...`` without pytest.
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claw_eval.harnesses._openclaw_bridge import (  # noqa: E402
    BridgeHandle,
    SchemaTranslationError,
    bridge_install,
    compile_plugin_source,
    derive_plugin_id,
    generate_and_install,
    json_schema_to_typebox,
)
from claw_eval.models.task import TaskDefinition  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures


SAMPLE_TASKS = [
    # Single-tool, single-service — the door-ticket experiment task.
    "T077_officeqa_highest_dept_spending",
    # Single tool with nested array + optional fields — exercises required/optional.
    "T086_pinbench_calendar_event_creation",
    # Empty tool set — must early-return.
    "C01zh_mortgage_prepay",
]


def _load(task_id: str) -> TaskDefinition:
    """Load a task YAML by id. Tests blow up loudly if the path is wrong."""
    path = TASKS_DIR / task_id / "task.yaml"
    assert path.exists(), f"task fixture missing: {path}"
    return TaskDefinition.from_yaml(path)


# ---------------------------------------------------------------------------
# Static test 1 — compile_plugin_source over real task.yaml samples


def test_compile_plugin_source_contains_expected_anchors() -> None:
    """For tasks with tool_endpoints, the rendered TS must reference every
    endpoint URL and tool name verbatim, plus the SDK / recorder boilerplate.
    """
    for task_id in ("T077_officeqa_highest_dept_spending",
                    "T086_pinbench_calendar_event_creation"):
        task = _load(task_id)
        src = compile_plugin_source(task)
        assert src is not None, f"{task_id} should produce source"

        # SDK + recorder imports — these are how the plugin reaches OpenClaw.
        assert "defineToolPlugin({" in src
        assert "recordCall({" in src
        assert "import { Type } from \"typebox\"" in src
        assert "from \"openclaw/plugin-sdk/tool-plugin\"" in src

        # Plugin id picked up the task id.
        assert task.task_id in src

        # Every tool name and endpoint URL must appear in source.
        for ep in task.tool_endpoints:
            assert ep.url in src, f"{task_id}: endpoint URL {ep.url!r} not in source"
            assert ep.tool_name in src, (
                f"{task_id}: tool_name {ep.tool_name!r} not in source"
            )
        for spec in task.tools:
            assert spec.name in src, f"{task_id}: tool spec name {spec.name!r} not in source"


def test_compile_plugin_source_empty_tools_returns_none() -> None:
    """C01zh_mortgage_prepay has ``tool_endpoints: []`` — sentinel path."""
    task = _load("C01zh_mortgage_prepay")
    assert task.tool_endpoints == []
    assert compile_plugin_source(task) is None


# ---------------------------------------------------------------------------
# host_gateway rewrite (bridge network mode — macOS compat)


def test_resolve_tool_url_rewrites_mock_host_when_gateway_set() -> None:
    """In bridge mode the container can't reach host mock services via
    localhost, so the mock URL host is rewritten to host.docker.internal
    (port + path unchanged)."""
    from claw_eval.harnesses._openclaw_bridge.generator import _resolve_tool_url

    task = _load("T077_officeqa_highest_dept_spending")
    ep = task.tool_endpoints[0]
    assert "localhost" in ep.url  # precondition: the fixture uses localhost

    url, _method = _resolve_tool_url(
        ep.tool_name, task, None, host_gateway="host.docker.internal"
    )
    assert "localhost" not in url
    assert url.startswith("http://host.docker.internal:")
    # port + path preserved
    assert url.endswith(ep.url.split("localhost", 1)[1])


def test_resolve_tool_url_verbatim_when_gateway_none() -> None:
    """host_gateway=None (host mode / default) -> URL unchanged. Linux
    zero-regression guard."""
    from claw_eval.harnesses._openclaw_bridge.generator import _resolve_tool_url

    task = _load("T077_officeqa_highest_dept_spending")
    ep = task.tool_endpoints[0]
    url, _method = _resolve_tool_url(ep.tool_name, task, None, host_gateway=None)
    assert url == ep.url


def test_compile_plugin_source_rewrites_host_in_bridge_mode() -> None:
    """End-to-end through compile: mock URL host rewritten, SANDBOX url kept."""
    task = _load("T077_officeqa_highest_dept_spending")
    src = compile_plugin_source(
        task,
        sandbox_url="http://localhost:8080",
        host_gateway="host.docker.internal",
    )
    assert src is not None
    assert "host.docker.internal:9121" in src
    assert "localhost:9121" not in src  # mock host fully rewritten


def test_compile_plugin_source_host_mode_unchanged() -> None:
    """No host_gateway -> mock URL verbatim (the existing behavior)."""
    task = _load("T077_officeqa_highest_dept_spending")
    src = compile_plugin_source(task)
    assert src is not None
    assert "localhost:9121" in src
    assert "host.docker.internal" not in src


# ---------------------------------------------------------------------------
# bridge network resolution (the in-container sandbox URL decision — R1)


def test_resolve_bridge_network_host_mode_default() -> None:
    """Empty env -> host mode: no gateway, plugin uses the host-mapped url."""
    from claw_eval.harnesses.openclaw import _resolve_bridge_network

    gw, url = _resolve_bridge_network(
        {}, sandbox_port=8080, host_sandbox_url="http://localhost:54545"
    )
    assert gw is None
    assert url == "http://localhost:54545"  # host-mapped, unchanged


def test_resolve_bridge_network_bridge_mode_uses_in_container_port() -> None:
    """CLAWEVAL_SANDBOX_NET=bridge -> SANDBOX_TOOLS target the IN-CONTAINER port
    (localhost:<sandbox_port>), NOT the host-mapped url. This is the R1 fix:
    using the host-mapped 54545 inside the container would 404 Bash/Read/Write."""
    from claw_eval.harnesses.openclaw import _resolve_bridge_network

    gw, url = _resolve_bridge_network(
        {"CLAWEVAL_SANDBOX_NET": "bridge"},
        sandbox_port=8080,
        host_sandbox_url="http://localhost:54545",
    )
    assert gw == "host.docker.internal"
    assert url == "http://localhost:8080"  # in-container port, NOT 54545


def test_resolve_bridge_network_case_insensitive_and_trimmed() -> None:
    from claw_eval.harnesses.openclaw import _resolve_bridge_network

    gw, url = _resolve_bridge_network(
        {"CLAWEVAL_SANDBOX_NET": "  BRIDGE  "},
        sandbox_port=8080,
        host_sandbox_url="http://localhost:1",
    )
    assert gw == "host.docker.internal"
    assert url == "http://localhost:8080"


def test_compile_plugin_source_is_deterministic() -> None:
    """Same task in -> same source out. Snapshot-style tests will rely on this."""
    task = _load("T077_officeqa_highest_dept_spending")
    a = compile_plugin_source(task)
    b = compile_plugin_source(task)
    assert a == b


def test_compile_plugin_source_handles_chinese_descriptions() -> None:
    """Tool descriptions / params often contain non-ASCII. ``json.dumps`` with
    ``ensure_ascii=False`` should pass them through unmolested.
    """
    task = _load("T077_officeqa_highest_dept_spending")
    # Synthesise a Chinese description and ensure it's rendered as-is.
    task.tools[0].description = "OCR 提取图片中的文字"
    src = compile_plugin_source(task)
    assert src is not None
    assert "OCR 提取图片中的文字" in src


# ---------------------------------------------------------------------------
# Static test 2 — json_schema_to_typebox primitive coverage


def test_typebox_primitives() -> None:
    assert json_schema_to_typebox({"type": "string"}) == "Type.String()"
    assert json_schema_to_typebox({"type": "integer"}) == "Type.Integer()"
    assert json_schema_to_typebox({"type": "number"}) == "Type.Number()"
    assert json_schema_to_typebox({"type": "boolean"}) == "Type.Boolean()"
    assert json_schema_to_typebox({"type": "null"}) == "Type.Null()"


def test_typebox_description_annotation() -> None:
    out = json_schema_to_typebox(
        {"type": "string", "description": "a path"}
    )
    assert out == 'Type.String({ description: "a path" })'


def test_typebox_array_of_strings() -> None:
    out = json_schema_to_typebox(
        {"type": "array", "items": {"type": "string"}}
    )
    assert out == "Type.Array(Type.String())"


def test_typebox_array_without_items_falls_back_to_any() -> None:
    out = json_schema_to_typebox({"type": "array"})
    assert out == "Type.Array(Type.Any())"


def test_typebox_object_with_required_and_optional() -> None:
    out = json_schema_to_typebox(
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        }
    )
    # Properties are sorted for determinism.
    assert out == (
        'Type.Object({ '
        '"attendees": Type.Optional(Type.Array(Type.String())), '
        '"title": Type.String()'
        ' })'
    )


def test_typebox_empty_object() -> None:
    assert json_schema_to_typebox({"type": "object"}) == "Type.Object({})"
    assert json_schema_to_typebox({}) == "Type.Object({})"
    assert json_schema_to_typebox(None) == "Type.Object({})"


def test_typebox_enum_strings() -> None:
    out = json_schema_to_typebox({"enum": ["asc", "desc"]})
    assert out == 'Type.Union([Type.Literal("asc"), Type.Literal("desc")])'


def test_typebox_enum_mixed_with_type() -> None:
    """``enum`` short-circuits; ``type`` is ignored when ``enum`` is present."""
    out = json_schema_to_typebox({"type": "string", "enum": ["a", "b"]})
    assert out == 'Type.Union([Type.Literal("a"), Type.Literal("b")])'


def test_typebox_enum_with_numbers_and_bool() -> None:
    out = json_schema_to_typebox({"enum": [1, 2.5, True, None]})
    # Booleans serialise to JS lowercase; None -> null.
    assert out == (
        "Type.Union([Type.Literal(1), Type.Literal(2.5), "
        "Type.Literal(true), Type.Literal(null)])"
    )


def test_typebox_nested_object() -> None:
    out = json_schema_to_typebox(
        {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                    },
                    "required": ["id"],
                },
            },
            "required": ["user"],
        }
    )
    assert out == (
        'Type.Object({ "user": Type.Object({ '
        '"id": Type.Integer(), "name": Type.Optional(Type.String())'
        ' }) })'
    )


# ---------------------------------------------------------------------------
# Static test 3 — unsupported constructs raise NotImplementedError


def test_typebox_rejects_oneof() -> None:
    with pytest.raises(NotImplementedError):
        json_schema_to_typebox(
            {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        )


def test_typebox_rejects_anyof() -> None:
    with pytest.raises(NotImplementedError):
        json_schema_to_typebox({"anyOf": [{"type": "string"}]})


def test_typebox_rejects_allof() -> None:
    with pytest.raises(NotImplementedError):
        json_schema_to_typebox({"allOf": [{"type": "string"}]})


def test_typebox_rejects_ref() -> None:
    with pytest.raises(NotImplementedError):
        json_schema_to_typebox({"$ref": "#/definitions/foo"})


def test_typebox_rejects_unknown_format() -> None:
    with pytest.raises(NotImplementedError):
        json_schema_to_typebox({"type": "string", "format": "ipv4"})


def test_typebox_known_formats_ok() -> None:
    # Whitelisted formats are dropped silently — TypeBox versions vary in
    # how they encode format hints.
    json_schema_to_typebox({"type": "string", "format": "date"})
    json_schema_to_typebox({"type": "string", "format": "uuid"})


def test_typebox_rejects_multi_type() -> None:
    with pytest.raises(NotImplementedError):
        json_schema_to_typebox({"type": ["string", "null"]})


def test_typebox_subclass_error_type() -> None:
    """``SchemaTranslationError`` is the precise type — make sure it's also a
    ``NotImplementedError`` so the spec-mandated catch works.
    """
    try:
        json_schema_to_typebox({"oneOf": []})
    except SchemaTranslationError as exc:
        assert isinstance(exc, NotImplementedError)
    else:  # pragma: no cover — guard against silent regressions
        pytest.fail("expected SchemaTranslationError")


# ---------------------------------------------------------------------------
# Static test 4 — BridgeHandle.cleanup() idempotency


def test_bridge_handle_cleanup_idempotent() -> None:
    """``cleanup()`` must be safe to call twice (and against missing dirs)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        state = base / "state"
        home = base / "home"
        plugin = base / "plugin"
        for p in (state, home, plugin):
            p.mkdir()
            (p / "marker").write_text("x")

        handle = BridgeHandle(
            case_state_dir=state,
            case_home_dir=home,
            plugin_id="claweval-bridge-test",
            traffic_log_path=base / "traffic.jsonl",
            plugin_dir=plugin,
        )

        # First call wipes the dirs.
        handle.cleanup()
        assert not state.exists()
        assert not home.exists()
        assert not plugin.exists()

        # Second call is a no-op.
        handle.cleanup()  # must not raise


def test_bridge_handle_cleanup_with_missing_dirs() -> None:
    """If the dirs were never created, cleanup() must not raise either."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        handle = BridgeHandle(
            case_state_dir=base / "never-existed-state",
            case_home_dir=base / "never-existed-home",
        )
        handle.cleanup()  # no exception


# ---------------------------------------------------------------------------
# Static test 5 — generate_and_install dry runs (skip_subprocess)


def test_generate_and_install_empty_tools_early_return() -> None:
    """Empty tool_endpoints -> sentinel handle, no plugin dir, but state dirs
    do get created (OpenClaw native runner needs them)."""
    task = _load("C01zh_mortgage_prepay")
    with tempfile.TemporaryDirectory() as tmp:
        case_dir = Path(tmp) / "case"
        handle = generate_and_install(task, case_dir, services_ctx=None)
        try:
            assert handle.plugin_id is None
            assert handle.traffic_log_path is None
            assert handle.plugin_dir is None
            # Isolation dirs are mandatory for the native runner.
            assert handle.case_state_dir.exists()
            assert handle.case_home_dir.exists()
            # No bridge_plugin subtree.
            assert not (case_dir / "bridge_plugin").exists()
        finally:
            handle.cleanup()


def test_generate_and_install_dry_run_produces_sources() -> None:
    """``skip_subprocess=True`` materialises the plugin dir but skips the
    npm / openclaw chain. Verifies the on-disk layout matches what the
    install chain expects."""
    task = _load("T077_officeqa_highest_dept_spending")
    with tempfile.TemporaryDirectory() as tmp:
        case_dir = Path(tmp) / "T077_case"
        handle = generate_and_install(
            task, case_dir, services_ctx=None,
            run_id="r0", skip_subprocess=True,
        )
        try:
            assert handle.plugin_id == (
                "claweval-bridge-T077_officeqa_highest_dept_spending-r0"
            )
            assert handle.plugin_dir is not None
            assert handle.plugin_dir.exists()
            # Files the install chain depends on.
            assert (handle.plugin_dir / "package.json").exists()
            assert (handle.plugin_dir / "tsconfig.json").exists()
            assert (handle.plugin_dir / "src" / "index.ts").exists()
            assert (handle.plugin_dir / "src" / "recorder.ts").exists()
            assert (handle.plugin_dir / "openclaw.plugin.json").exists()
            # Traffic log pre-created so recorder.ts can append.
            assert handle.traffic_log_path is not None
            assert handle.traffic_log_path.exists()
            # Source got the plugin id and tool url.
            src_text = (handle.plugin_dir / "src" / "index.ts").read_text(encoding="utf-8")
            assert handle.plugin_id in src_text
            assert "http://localhost:9121/ocr/extract" in src_text
            # Manifest carries the same tool list as task.tool_endpoints.
            import json as _json
            manifest = _json.loads((handle.plugin_dir / "openclaw.plugin.json").read_text())
            assert manifest["id"] == handle.plugin_id
            assert manifest["contracts"]["tools"] == ["ocr_extract_text"]
        finally:
            handle.cleanup()
        # After cleanup, everything is gone.
        assert not (case_dir / "bridge_plugin").exists()


def test_bridge_install_context_manager_cleans_up_on_success() -> None:
    task = _load("T077_officeqa_highest_dept_spending")
    with tempfile.TemporaryDirectory() as tmp:
        case_dir = Path(tmp) / "case"
        with bridge_install(task, case_dir, skip_subprocess=True) as handle:
            assert handle.plugin_dir is not None
            assert handle.plugin_dir.exists()
            captured_plugin_dir = handle.plugin_dir
        # After the with-block, dirs are removed.
        assert not captured_plugin_dir.exists()


def test_bridge_install_context_manager_cleans_up_on_failure() -> None:
    """Body raising must still trigger cleanup."""
    task = _load("T077_officeqa_highest_dept_spending")
    with tempfile.TemporaryDirectory() as tmp:
        case_dir = Path(tmp) / "case"
        captured: dict[str, Path] = {}
        with pytest.raises(RuntimeError, match="boom"):
            with bridge_install(task, case_dir, skip_subprocess=True) as handle:
                captured["plugin_dir"] = handle.plugin_dir  # type: ignore[assignment]
                raise RuntimeError("boom")
        assert not captured["plugin_dir"].exists()


# ---------------------------------------------------------------------------
# Static test 6 — derive_plugin_id sanitisation


def test_derive_plugin_id_sanitises() -> None:
    # Dots/slashes in task ids and run ids get folded to dashes; the prefix
    # never changes.
    assert derive_plugin_id("T077", "abc123") == "claweval-bridge-T077-abc123"
    assert (
        derive_plugin_id("T077.foo/bar", "run id with spaces")
        == "claweval-bridge-T077-foo-bar-run-id-with-spaces"
    )
    # Falls back to defaults when sanitisation strips everything.
    assert derive_plugin_id("///", "...") == "claweval-bridge-task-run"


# ---------------------------------------------------------------------------
# Wave 3-E (§3.4a) — sandbox_url + SANDBOX_TOOLS routing


def test_compile_plugin_source_routes_bash_to_sandbox_url() -> None:
    """T068 has Bash + web_search + web_fetch. With sandbox_url set,
    Bash must resolve to ``{sandbox_url}/exec`` (the sandbox server route)
    while the HTTP tools keep their declared mock-service URLs.
    """
    task = _load("T068zh_llama_w8a8_cuda_bug")
    src = compile_plugin_source(task, sandbox_url="http://localhost:8080")
    assert src is not None
    # Bash must be routed to the sandbox server.
    assert "http://localhost:8080/exec" in src, (
        "Bash should route to sandbox_url/exec (SANDBOX_ENDPOINTS['Bash'])"
    )
    # The host-side mock services keep their declared URLs unchanged.
    assert "http://localhost:9114/web/search" in src
    assert "http://localhost:9114/web/fetch" in src
    # The Bash tool name and at least one HTTP tool name must appear.
    assert '"Bash"' in src
    assert '"web_search"' in src


def test_compile_plugin_source_bash_without_sandbox_url_raises() -> None:
    """A task containing a SANDBOX_TOOL but no ``sandbox_url`` argument must
    raise ``SchemaTranslationError`` at source-render time — this is the
    fail-fast guard before plugin install kicks off."""
    task = _load("T068zh_llama_w8a8_cuda_bug")
    with pytest.raises(SchemaTranslationError):
        compile_plugin_source(task, sandbox_url=None)


def test_compile_plugin_source_no_sandbox_tool_ignores_sandbox_url() -> None:
    """Tasks with only HTTP mock-service tools (no SANDBOX_TOOLS) should
    render identically regardless of ``sandbox_url`` — backward compat for
    Wave 3-D host mode."""
    task = _load("T077_officeqa_highest_dept_spending")
    a = compile_plugin_source(task)
    b = compile_plugin_source(task, sandbox_url="http://localhost:8080")
    assert a == b


def test_bridgeable_tools_includes_sandbox_tools() -> None:
    """Internal: the bridgeable-tool computation must surface Bash for T068
    even though it has no ``tool_endpoints`` entry."""
    from claw_eval.harnesses._openclaw_bridge.generator import _bridgeable_tools  # noqa: E402
    task = _load("T068zh_llama_w8a8_cuda_bug")
    names = _bridgeable_tools(task)
    assert "Bash" in names
    assert "web_search" in names
    assert "web_fetch" in names
    # task.tools order must come first.
    assert names.index("Bash") < names.index("web_search")


def test_generate_and_install_t068_dry_run_with_sandbox_url() -> None:
    """T068 has Bash; dry-run with sandbox_url must materialise a plugin
    whose source carries the sandbox endpoint AND the manifest lists Bash."""
    task = _load("T068zh_llama_w8a8_cuda_bug")
    with tempfile.TemporaryDirectory() as tmp:
        case_dir = Path(tmp) / "T068_case"
        handle = generate_and_install(
            task, case_dir, services_ctx=None,
            run_id="r0", skip_subprocess=True,
            sandbox_url="http://localhost:8080",
        )
        try:
            assert handle.plugin_id == (
                "claweval-bridge-T068zh_llama_w8a8_cuda_bug-r0"
            )
            assert handle.plugin_dir is not None
            src_text = (handle.plugin_dir / "src" / "index.ts").read_text(
                encoding="utf-8"
            )
            assert "http://localhost:8080/exec" in src_text
            assert "http://localhost:9114/web/search" in src_text
            # Manifest captures every bridgeable tool.
            import json as _json
            manifest = _json.loads(
                (handle.plugin_dir / "openclaw.plugin.json").read_text()
            )
            assert "Bash" in manifest["contracts"]["tools"]
        finally:
            handle.cleanup()


def test_sandbox_endpoints_aligned_with_dispatcher() -> None:
    """``SANDBOX_ENDPOINTS`` is a re-export of the dispatcher's path map;
    if the dispatcher gains/renames a path, the bridge must follow."""
    from claw_eval.harnesses._openclaw_bridge import SANDBOX_ENDPOINTS  # noqa: E402
    from claw_eval.runner.sandbox_dispatcher import SandboxToolDispatcher  # noqa: E402

    assert SANDBOX_ENDPOINTS == dict(SandboxToolDispatcher._PATH_MAP)
    # Spot-check the routes the design doc names.
    assert SANDBOX_ENDPOINTS["Bash"] == "/exec"
    assert SANDBOX_ENDPOINTS["Read"] == "/read"
    assert SANDBOX_ENDPOINTS["Write"] == "/write"


def test_run_install_chain_retries_transient_npm_network_error(
    monkeypatch, tmp_path
) -> None:
    """``npm install`` transient network failures should be retried once.

    Real failure seen in smoke: ECONNRESET / ``npm error network aborted``.
    The rest of the chain should continue once a retry succeeds.
    """
    from claw_eval.harnesses._openclaw_bridge import generator as mod

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    calls: list[list[str]] = []
    npm_attempts = 0

    def fake_run(cmd, **kwargs):
        nonlocal npm_attempts
        calls.append(list(cmd))
        if list(cmd)[:2] == ["npm", "install"]:
            npm_attempts += 1
            if npm_attempts == 1:
                raise subprocess.CalledProcessError(
                    1,
                    cmd,
                    stderr="npm error code ECONNRESET\nnpm error network aborted\n",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    mod._run_install_chain(plugin_dir=plugin_dir, plugin_id="bridge-p1", env={})

    assert npm_attempts == 2
    assert calls[:2] == [["npm", "install"], ["npm", "install"]]
    assert ["openclaw", "plugins", "enable", "bridge-p1"] in calls


def test_run_install_chain_does_not_retry_non_network_npm_error(
    monkeypatch, tmp_path
) -> None:
    """Non-network npm failures should still fail fast."""
    from claw_eval.harnesses._openclaw_bridge import generator as mod

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    npm_attempts = 0

    def fake_run(cmd, **kwargs):
        nonlocal npm_attempts
        if list(cmd)[:2] == ["npm", "install"]:
            npm_attempts += 1
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="npm error code EJSONPARSE\nnpm error package.json parse failed\n",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        mod._run_install_chain(plugin_dir=plugin_dir, plugin_id="bridge-p1", env={})

    assert npm_attempts == 1


# ---------------------------------------------------------------------------
# Standalone runner — usable when pytest is unavailable.


def _run_all() -> None:
    import inspect
    failed = 0
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    for name, fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
        else:
            print(f"OK   {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
