"""Tests for grading with the judge disabled (``--no-judge``).

Covers the blocker documented in ``docs/rollout_*_5task.md`` §2 / §8: several
graders (the officeqa family T076-T085, and ~57 others) call
``judge.evaluate()`` unconditionally. With ``--no-judge`` the judge is ``None``,
so grading crashed with
``AttributeError: 'NoneType' object has no attribute 'evaluate'``, forcing every
rollout to run with a judge enabled.

The fix is framework-level: the single grading chokepoint
(``_grade_with_optional_params``) substitutes a :class:`NoJudge` null-object for
``None``, so judge-dependent components contribute a neutral 0.0 sub-score
instead of crashing.
"""

from __future__ import annotations

from pathlib import Path

from claw_eval.cli import _grade_with_optional_params
from claw_eval.graders.llm_judge import JudgeResult, NoJudge
from claw_eval.graders.registry import get_grader
from claw_eval.models.message import Message
from claw_eval.models.task import TaskDefinition
from claw_eval.models.trace import DimensionScores, ToolDispatch, TraceMessage

REPO_ROOT = Path(__file__).resolve().parent.parent
T077_DIR = REPO_ROOT / "tasks" / "T077_officeqa_highest_dept_spending"
T002_DIR = REPO_ROOT / "tasks" / "T002_email_triage"


def _msg(role: str, text: str) -> TraceMessage:
    return TraceMessage(trace_id="t", message=Message(role=role, content=text))


def test_nojudge_null_object_returns_neutral_zero():
    nj = NoJudge()
    for result in (
        nj.evaluate("prompt", "convo", "actions", "rubric"),
        nj.evaluate_actions("prompt", "artifacts", "rubric"),
        nj.evaluate_visual("rubric", [], []),
    ):
        assert isinstance(result, JudgeResult)
        assert result.score == 0.0
    # The neutral evaluations are recorded so downstream tooling can see them.
    assert len(nj.get_call_log()) == 3
    assert all(c["no_judge"] for c in nj.get_call_log())
    nj.reset_call_log()
    assert nj.get_call_log() == []


def test_officeqa_grader_does_not_crash_with_judge_none():
    """The real T077 officeqa grader calls judge.evaluate() unconditionally.
    With judge=None it must NOT crash and must return valid DimensionScores."""
    grader = get_grader("T077_officeqa_highest_dept_spending", task_dir=T077_DIR)
    task = TaskDefinition.from_yaml(T077_DIR / "task.yaml")

    # A trace where the agent used OCR and stated the correct answer (36080).
    messages = [
        _msg("user", task.prompt.text),
        _msg("assistant", "The highest spending department in FY1955 was Defense at 36,080 million dollars."),
    ]
    dispatches = [
        ToolDispatch(
            trace_id="t", tool_use_id="u1", tool_name="ocr_extract_text",
            endpoint_url="http://localhost/ocr", response_status=200,
        )
    ]

    scores, judge_calls = _grade_with_optional_params(
        grader, messages, dispatches, task,
        audit_data={}, judge=None, media_events=[],
    )

    assert isinstance(scores, DimensionScores)
    # Valid, bounded scores (no crash, no garbage).
    assert 0.0 <= scores.completion <= 1.0
    assert 0.0 <= scores.robustness <= 1.0
    assert scores.safety in (0.0, 1.0)
    # Rule-based parts (numerical match 0.55 + OCR usage 0.10) still contribute;
    # only the judge sub-score (0.35) is the neutral 0.0. So completion is
    # partial, NOT a silent pass and NOT a crash.
    assert scores.completion > 0.0
    assert scores.completion < 1.0
    # The skipped judge evaluation was recorded as a no_judge neutral call.
    assert judge_calls and all(c.get("no_judge") for c in judge_calls)


def test_grade_chokepoint_uses_real_judge_when_provided():
    """When a real judge object is passed, it is used unchanged (regression guard
    that the NoJudge substitution only triggers on None)."""
    grader = get_grader("T077_officeqa_highest_dept_spending", task_dir=T077_DIR)
    task = TaskDefinition.from_yaml(T077_DIR / "task.yaml")
    messages = [_msg("assistant", "Defense, 36080 million")]
    dispatches: list[ToolDispatch] = []

    class _FakeJudge:
        def __init__(self):
            self.calls = 0

        def reset_call_log(self):
            pass

        def get_call_log(self):
            return []

        def evaluate(self, *a, **k):
            self.calls += 1
            return JudgeResult(score=1.0, reasoning="ok")

    fake = _FakeJudge()
    scores, _ = _grade_with_optional_params(
        grader, messages, dispatches, task,
        audit_data={}, judge=fake, media_events=[],
    )
    assert fake.calls == 1  # the provided judge was actually used
    assert isinstance(scores, DimensionScores)


def test_email_triage_direct_client_grader_no_hang_with_judge_none():
    """The email-triage grader bypasses judge.evaluate() and accesses
    judge.client directly inside a 30-attempt retry loop. With judge disabled it
    must short-circuit (NOT hang retrying / crash). Integration check uncovered
    this: NoJudge has no .client, so the grader must guard on judge being
    disabled rather than letting the AttributeError fall into the retry loop."""
    import time

    grader = get_grader("T002_email_triage", task_dir=T002_DIR)
    task = TaskDefinition.from_yaml(T002_DIR / "task.yaml")
    messages = [
        _msg("user", task.prompt.text),
        _msg("assistant", "msg_001 needs reply; msg_003 is FYI; msg_004 is spam."),
    ]
    dispatches = [
        ToolDispatch(
            trace_id="t", tool_use_id="u1", tool_name="gmail_list_messages",
            endpoint_url="http://localhost/gmail", response_status=200,
        ),
    ]

    t0 = time.time()
    scores, _ = _grade_with_optional_params(
        grader, messages, dispatches, task,
        audit_data={}, judge=None, media_events=[],
    )
    elapsed = time.time() - t0

    assert isinstance(scores, DimensionScores)
    assert elapsed < 5.0, f"grading took {elapsed:.1f}s — retry loop not short-circuited"
    # LLM-classification component (0.65) is neutral 0.0; tool-usage (0.15)
    # still contributes from the gmail_list_messages call. So completion is
    # partial, not crashed and not a silent pass.
    assert 0.0 <= scores.completion <= 1.0
    assert scores.completion > 0.0  # rule-based tool component still counted
