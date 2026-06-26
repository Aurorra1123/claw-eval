"""Tests for ``_resolve_tasks_dir`` symlink resolution.

Covers the blocker documented in ``docs/rollout_*_5task.md`` §2.2 / §5.3: when a
task dir is a *symlink*, ``_resolve_tasks_dir`` used ``task_yaml.parent.parent``
without ``.resolve()``, so ``tasks_dir.parent`` (the mock-service CWD) landed in
the symlink's parent (e.g. /tmp) instead of the repo root — breaking the
relative ``python mock_services/.../server.py`` service commands.
"""

from __future__ import annotations

from pathlib import Path

from claw_eval.cli import _resolve_tasks_dir


def test_resolve_tasks_dir_real_dir(tmp_path: Path):
    """A normal (non-symlink) task dir: tasks_dir is <root>/tasks, cwd is <root>."""
    root = tmp_path / "repo"
    real_tasks = root / "tasks"
    task_dir = real_tasks / "T002_email_triage"
    task_dir.mkdir(parents=True)
    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text("task_id: T002_email_triage\n")

    tasks_dir = _resolve_tasks_dir(task_yaml)
    assert tasks_dir == real_tasks.resolve()
    # cwd used for ServiceManager(cwd=tasks_dir.parent) must be the repo root
    assert tasks_dir.parent == root.resolve()


def test_resolve_tasks_dir_symlinked_dir_lands_at_real_root(tmp_path: Path):
    """A symlinked task dir must resolve to its real location so the mock-service
    CWD (tasks_dir.parent) is the *real* repo root, not the symlink's parent."""
    # Real repo layout: <repo>/tasks/T002_email_triage/task.yaml + <repo>/mock_services
    repo = tmp_path / "repo"
    real_task_dir = repo / "tasks" / "T002_email_triage"
    real_task_dir.mkdir(parents=True)
    real_yaml = real_task_dir / "task.yaml"
    real_yaml.write_text("task_id: T002_email_triage\n")
    (repo / "mock_services").mkdir()  # only exists at the real repo root

    # A separate "selection" dir containing a symlink to the real task dir
    # (the copy/symlink workaround rollouts used).
    sel_dir = tmp_path / "selection"
    sel_dir.mkdir()
    symlinked_task = sel_dir / "T002_email_triage"
    symlinked_task.symlink_to(real_task_dir, target_is_directory=True)

    symlinked_yaml = symlinked_task / "task.yaml"
    assert symlinked_yaml.exists()  # symlink is followed

    tasks_dir = _resolve_tasks_dir(symlinked_yaml)

    # Without .resolve(), tasks_dir.parent would be `selection` (no mock_services).
    # With the fix, it lands at the real repo root where mock_services/ lives.
    cwd = tasks_dir.parent
    assert cwd == repo.resolve()
    assert (cwd / "mock_services").exists()
    # And it is NOT the symlink's parent dir.
    assert cwd != sel_dir.resolve()
