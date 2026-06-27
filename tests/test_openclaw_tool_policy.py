"""Unit tests for OpenClawHarness._write_tool_policy_config (tool isolation).

The §6.5 contract: during a task the model's ONLY visible tool set is the
bridge plugin's tools. The original implementation used ``tools.deny`` (deny
the ~40 builtins) + a ``discard`` to un-deny bridge tools that COLLIDE with a
builtin name (web_search/web_fetch). That collision handling is the bug: on
OpenClaw 2026.6.9 ``tools.deny`` matches by normalized bare tool NAME with no
source dimension (tool-policy: buildPluginToolGroups + collectExplicitDenylist),
so un-denying ``web_search`` revives the BUILTIN web_search which hijacks the
call — the bridge mock never sees it.

The fix is an ALLOW allowlist: list exactly the bridge tools (+ minimal
scaffolding) so every builtin — including a same-named one — is excluded, and
the bridge tool is the only ``web_search`` that resolves.

Runnable both under pytest and as ``python tests/test_openclaw_tool_policy.py``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claw_eval.harnesses.openclaw import OpenClawHarness  # noqa: E402

# Minimal scaffolding the agent loop needs even under a strict allowlist.
_SCAFFOLD = {"session_status", "multi_tool_use.parallel"}


def _write(tmp: Path, bridge_tool_names, plugin_id="claweval-bridge-x", existing=None):
    cfg = tmp / "openclaw.json"
    if existing is not None:
        cfg.write_text(json.dumps(existing), encoding="utf-8")
    OpenClawHarness._write_tool_policy_config(
        config_path=cfg,
        bridge_tool_names=list(bridge_tool_names),
        bridge_plugin_id=plugin_id,
    )
    return json.loads(cfg.read_text(encoding="utf-8"))


def test_allow_lists_bridge_tools_plus_scaffolding():
    with tempfile.TemporaryDirectory() as d:
        out = _write(Path(d), ["gmail_send_message", "contacts_list"])
    allow = set(out["tools"]["allow"])
    assert {"gmail_send_message", "contacts_list"} <= allow
    assert _SCAFFOLD <= allow


def test_colliding_bridge_tool_is_allowed_not_specially_discarded():
    # web_search collides with a builtin. Under allow, it must simply be in the
    # allowlist (so the bridge tool resolves); there is no deny-discard dance.
    with tempfile.TemporaryDirectory() as d:
        out = _write(Path(d), ["web_search", "web_fetch"])
    allow = set(out["tools"]["allow"])
    assert {"web_search", "web_fetch"} <= allow
    # No deny list is needed; if present it must NOT contain the bridge tools.
    deny = set(out.get("tools", {}).get("deny") or [])
    assert "web_search" not in deny
    assert "web_fetch" not in deny


def test_allow_excludes_arbitrary_builtins():
    # A builtin the task does NOT bridge (e.g. exec) must not be allowed.
    with tempfile.TemporaryDirectory() as d:
        out = _write(Path(d), ["web_search"])
    allow = set(out["tools"]["allow"])
    assert "exec" not in allow
    assert "read" not in allow
    assert "write" not in allow


def test_colliding_web_tools_disable_managed_web_so_bridge_takes_over():
    # web_search / web_fetch are OpenClaw MANAGED web tools (bundled). An
    # allowlist makes the bridge tool visible, but at DISPATCH time the managed
    # builtin still wins (stderr: "web_search is disabled or no provider").
    # The fix: disable the managed web tools so the same-named bridge tool is
    # the only one that resolves.
    with tempfile.TemporaryDirectory() as d:
        out = _write(Path(d), ["web_search", "web_fetch", "send_notification"])
    web = out["tools"]["web"]
    assert web["search"]["enabled"] is False
    assert web["fetch"]["enabled"] is False


def test_no_web_collision_leaves_managed_web_untouched():
    # A task whose bridge tools don't collide with managed web tools must not
    # touch tools.web (no gratuitous config).
    with tempfile.TemporaryDirectory() as d:
        out = _write(Path(d), ["gmail_list_messages", "helpdesk_list_tickets"])
    assert "web" not in out.get("tools", {})


def test_preserves_plugins_allow_and_merges_existing():
    with tempfile.TemporaryDirectory() as d:
        out = _write(
            Path(d),
            ["web_search"],
            plugin_id="claweval-bridge-T044",
            existing={"models": {"keep": 1}, "tools": {"allow": ["pre_existing"]}},
        )
    # bridge plugin registered for the policy filter
    assert "claweval-bridge-T044" in out["plugins"]["allow"]
    # unrelated top-level keys survive
    assert out["models"] == {"keep": 1}
    # a pre-existing allow entry is preserved (union, not clobbered)
    assert "pre_existing" in set(out["tools"]["allow"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
