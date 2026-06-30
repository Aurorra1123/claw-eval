"""Tests for ``batch --task-ids`` arbitrary multi-task selection.

Covers the ergonomic blocker documented in ``docs/rollout_*_5task.md`` §2.1 /
§5.1: ``batch`` previously had no way to select a non-contiguous set of task
IDs (e.g. {T002, T008, T012, T018, T077}), forcing rollout subagents to
copy/symlink task dirs as a workaround. ``--task-ids`` removes that need.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from claw_eval import cli


def _make_fake_tasks(root: Path, names: list[str]) -> None:
    """Create minimal task dirs (each just needs a task.yaml to be discovered)."""
    for name in names:
        d = root / name
        d.mkdir(parents=True)
        # cmd_batch discovery only needs (d / "task.yaml") to exist for selection;
        # --tag is the only selector that parses the YAML, and we don't use it here.
        (d / "task.yaml").write_text(
            f"task_id: {name}\ntask_name: {name}\ndifficulty: easy\n"
        )


def _batch_args(tasks_dir: Path, **overrides) -> argparse.Namespace:
    base = dict(
        tasks_dir=str(tasks_dir),
        filter=None,
        tag=None,
        range=None,
        task_ids=None,
        parallel=2,
        model=None,
        api_key=None,
        base_url=None,
        config=None,
        trials=1,
        trace_dir=None,
        judge_model=None,
        no_judge=True,
        proxy=None,
        port_base_offset=0,
        sandbox=False,
        sandbox_image=None,
        sandbox_tools=False,
        harness="claweval",
        rerun_errors=None,
        continue_dir=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def tasks_root(tmp_path: Path) -> Path:
    root = tmp_path / "tasks"
    _make_fake_tasks(
        root,
        [
            "T002_email_triage",
            "T008_todo_management",
            "T012_expense_report",
            "T018_ticket_triage",
            "T077_officeqa_highest_dept_spending",
            "T099_unrelated",
        ],
    )
    return root


def _capture_selected_task_dirs(monkeypatch) -> list[str]:
    """Patch ProcessPoolExecutor + as_completed so cmd_batch seeds all initial
    submissions (one per slot) and then stops deterministically, recording the
    exact set of task dirs it would have run."""
    captured: list[str] = []

    class _StopBatch(Exception):
        pass

    class _FakeFuture:
        pass

    class _FakePool:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, *, task_dir=None, **k):
            captured.append(task_dir)
            return _FakeFuture()

    def _fake_as_completed(pending):
        # All initial slots have been seeded by now; bail before doing real work.
        raise _StopBatch

    monkeypatch.setattr(cli, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(cli, "as_completed", _fake_as_completed)
    return captured, _StopBatch


def test_task_ids_selects_exact_noncontiguous_set(tasks_root, tmp_path, monkeypatch):
    captured, stop = _capture_selected_task_dirs(monkeypatch)
    args = _batch_args(
        tasks_root,
        trace_dir=str(tmp_path / "traces"),
        task_ids="T002_email_triage,T008_todo_management,T012_expense_report,"
                 "T018_ticket_triage,T077_officeqa_highest_dept_spending",
        parallel=10,  # enough slots that all 5 get submitted before pool teardown
    )
    try:
        cli.cmd_batch(args)
    except stop:
        pass

    selected = {Path(p).name for p in captured}
    assert selected == {
        "T002_email_triage",
        "T008_todo_management",
        "T012_expense_report",
        "T018_ticket_triage",
        "T077_officeqa_highest_dept_spending",
    }
    assert "T099_unrelated" not in selected


def test_task_ids_short_numeric_form_resolves(tasks_root, tmp_path, monkeypatch):
    captured, stop = _capture_selected_task_dirs(monkeypatch)
    args = _batch_args(
        tasks_root, trace_dir=str(tmp_path / "traces"),
        task_ids="T002,T077", parallel=10,
    )
    try:
        cli.cmd_batch(args)
    except stop:
        pass
    selected = {Path(p).name for p in captured}
    assert selected == {"T002_email_triage", "T077_officeqa_highest_dept_spending"}


def test_task_ids_unknown_id_errors_and_lists_it(tasks_root, capsys):
    args = _batch_args(tasks_root, task_ids="T002_email_triage,T999_does_not_exist")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_batch(args)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "T999_does_not_exist" in out
    assert "not found" in out.lower()


def test_task_ids_mutually_exclusive_with_range(tasks_root, capsys):
    args = _batch_args(tasks_root, task_ids="T002", range="1-10")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_batch(args)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "--range" in out
    assert "mutually exclusive" in out.lower()


def test_task_ids_mutually_exclusive_with_filter(tasks_root, capsys):
    args = _batch_args(tasks_root, task_ids="T002", filter="email")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_batch(args)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "--filter" in out
