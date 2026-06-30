"""Wave 3-F §6.8 — placeholder harness contracts + Phase 4 registry expansion.

These tests verify the codex / claudecode stubs:

* registered in ``harnesses._REGISTRY``,
* preflight returns a non-empty error list,
* run() raises ``NotImplementedError``,
* declare the Protocol surface (name / supported_features / preflight / run).

Plus a Phase 4 Wave 4-D check that the AOrchestra harness is registered as a
first-class harness (not a placeholder) and preflights a no-tool task cleanly.

Cheap and static — they make sure the slots are reserved correctly so future
work on Codex / Claude Code starts from a sound base.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from claw_eval.harnesses import get_harness
from claw_eval.harnesses.aorchestra import AOrchestraHarness
from claw_eval.harnesses.base import HarnessResult
from claw_eval.harnesses.claudecode import ClaudeCodeHarness
from claw_eval.harnesses.codex import CodexHarness
from claw_eval.models.task import TaskDefinition


PLACEHOLDER_HARNESSES = [
    ("codex", CodexHarness),
    ("claudecode", ClaudeCodeHarness),
]


@pytest.mark.parametrize("name,cls", PLACEHOLDER_HARNESSES)
def test_placeholder_registered(name: str, cls: type) -> None:
    """The registry returns the right instance for each placeholder name."""
    h = get_harness(name)
    assert isinstance(h, cls)
    assert h.name == name


@pytest.mark.parametrize("name,cls", PLACEHOLDER_HARNESSES)
def test_placeholder_supported_features_empty(name: str, cls: type) -> None:
    """Until the real impl lands, no task.yaml feature is honoured."""
    h = cls()
    assert h.supported_features == frozenset(), (
        f"{name}: supported_features must be empty until real implementation lands"
    )


@pytest.mark.parametrize("name,cls", PLACEHOLDER_HARNESSES)
def test_placeholder_preflight_rejects(name: str, cls: type) -> None:
    """Preflight rejects every task with a clear "not implemented" message."""
    task = TaskDefinition.from_yaml(
        Path(__file__).parent.parent / "tasks" / "T077_officeqa_highest_dept_spending" / "task.yaml"
    )
    h = cls()
    errs = h.preflight(task)
    assert errs, f"{name}: preflight must return non-empty errors"
    assert any("not implemented" in e.lower() for e in errs), (
        f"{name}: preflight error must mention 'not implemented'"
    )


@pytest.mark.parametrize("name,cls", PLACEHOLDER_HARNESSES)
def test_placeholder_run_raises(name: str, cls: type) -> None:
    """run() must raise NotImplementedError, not silently produce a bogus trace."""
    h = cls()
    with pytest.raises(NotImplementedError):
        h.run(
            task=None,                # type: ignore[arg-type]
            trace_dir=Path("/tmp"),
            run_id="placeholder-check",
            cfg=None,                 # type: ignore[arg-type]
            sandbox_handle=None,
            user_agent=None,
            services_ctx=None,
        )


@pytest.mark.parametrize("name,cls", PLACEHOLDER_HARNESSES)
def test_placeholder_run_signature_matches_protocol(name: str, cls: type) -> None:
    """The run() signature must accept the same kwargs as ClawEvalHarness /
    OpenClawHarness so the CLI / future wiring doesn't break when a real
    implementation lands."""
    sig = inspect.signature(cls().run)
    required_kwargs = {
        "trace_dir", "run_id", "cfg",
        "sandbox_handle", "user_agent", "services_ctx",
    }
    actual = set(sig.parameters.keys()) - {"self"}
    missing = required_kwargs - actual
    assert not missing, f"{name}: run() missing kwargs {missing}"
    # ``task`` should be positional-or-keyword as the first arg
    first = list(sig.parameters.values())[0]
    assert first.name == "task"


def test_registry_exposes_all_five_harnesses() -> None:
    """End-state sanity check: registry surface is what the CLI expects."""
    from claw_eval.harnesses import _REGISTRY

    assert set(_REGISTRY.keys()) == {
        "aorchestra", "claweval", "openclaw", "codex", "claudecode",
    }


def test_aorchestra_registered_and_preflights_pure_text_task() -> None:
    """Phase 4 Wave 4-D: AOrchestra is registered as a first-class harness
    (not a placeholder) and preflights a no-tool, no-user-agent task cleanly.

    T091 is the canonical pure-text fixture: ``tools: []`` /
    ``tool_endpoints: []`` / no ``user_agent`` block. The harness should
    accept it without error.
    """
    h = get_harness("aorchestra")
    assert isinstance(h, AOrchestraHarness)
    assert h.name == "aorchestra"
    # AOrchestra is a real implementation, so it MUST advertise both http and
    # sandbox features.
    assert "http_services" in h.supported_features
    assert "sandbox_tools" in h.supported_features

    task = TaskDefinition.from_yaml(
        Path(__file__).parent.parent
        / "tasks" / "T091_pinbench_humanize_blog" / "task.yaml"
    )
    errs = h.preflight(task)
    assert errs == [], f"AOrchestra should preflight T091 cleanly: {errs}"
