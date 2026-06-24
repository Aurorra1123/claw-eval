"""Host-workdir env_snapshot — the OpenClaw analogue of cli.py:_collect_env_snapshot.

Phase 3 Wave 3-D §3.6 — see ``docs/harness_design.md``.

The container path (``cli.py:_collect_env_snapshot``) reaches into a running
sandbox container via HTTP ``/exec`` + ``/glob`` + ``/read``. The OpenClaw
harness has no container; it runs the agent in a host workdir. This module
ports the same four-step pipeline (inject grader files -> run snapshot
commands -> read snapshot files -> read local grader files) to the host so
graders see byte-identical snapshot ``dict`` schemas regardless of harness.

Schema parity (matched against cli.py:191-202 / 222-250):

* ``cmd:<command>``      -> ``{"stdout": str, "stderr": str, "exit_code": int}``
                           or ``{"error": str}`` on failure.
* ``file:<rel_path>``    -> ``{"content": str, "encoding": "base64",
                              "mime_type": str}`` or ``{"error": str}``.
* ``local_file:<rel>``   -> same as ``file:`` but read from ``task_dir``.

Best-effort semantics: per-entry failures are captured as ``{"error": ...}``
entries; we never raise out of the snapshot loop. This matches the container
path, which would let the next entry succeed even if one ``/read`` 500'd.
"""

from __future__ import annotations

import base64
import glob
import logging
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.task import TaskDefinition

__all__ = ["inject_grader_files_host", "collect_workdir_snapshot"]

_log = logging.getLogger(__name__)


# Maximum files per glob pattern. Matches cli.py:228 ``max_files: 50``
# semantics so graders see the same cap regardless of harness.
_GLOB_MAX_FILES = 50


def inject_grader_files_host(
    task: "TaskDefinition",
    work_dir: Path,
    task_dir: Path,
) -> int:
    """Copy ``task.sandbox_grader_files`` into ``work_dir`` after the agent exits.

    Mirrors ``SandboxRunner.inject_grader_files`` for the host workdir case.
    Returns the count of files successfully copied. Missing source files are
    logged but do not abort the loop — same best-effort policy as the
    container path.

    The CALLER must guarantee the OpenClaw subprocess has already exited; this
    function does not enforce ordering. See ``OpenClawHarness.run`` for the
    invocation point (§3.6 step 1: "before snapshot, after agent exit").
    """
    file_list = list(getattr(task, "sandbox_grader_files", None) or [])
    if not file_list:
        return 0

    work_dir = Path(work_dir)
    task_dir = Path(task_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    n_copied = 0
    for rel in file_list:
        src = task_dir / rel
        dst = work_dir / rel
        if not src.exists():
            _log.warning("grader file source missing: %s", src)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n_copied += 1
        except OSError as exc:
            _log.warning("grader file copy failed (%s -> %s): %s", src, dst, exc)
    return n_copied


def collect_workdir_snapshot(
    work_dir: Path,
    task: "TaskDefinition",
    task_dir: Path,
) -> dict[str, Any]:
    """Run env_snapshot_commands + read env_snapshot_files + local_grader_files.

    Order matches the container path (cli.py:184 onwards):
    1. commands first — they often generate the files we then read.
    2. files next, with glob expansion.
    3. local grader files last (read from ``task_dir``, not ``work_dir``).

    Returns a snapshot ``dict`` with keys following the schema in this
    module's docstring. Never raises; per-entry failures land under
    ``{"error": ...}``.
    """
    work_dir = Path(work_dir)
    task_dir = Path(task_dir)
    snapshot: dict[str, Any] = {}

    timeout_s = getattr(getattr(task, "environment", None), "env_snapshot_timeout", 10)

    # ---- Step 1: env_snapshot_commands ----
    for cmd in getattr(task, "env_snapshot_commands", None) or []:
        key = f"cmd:{cmd}"
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            snapshot[key] = {
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "exit_code": int(proc.returncode),
            }
        except subprocess.TimeoutExpired as exc:
            # cli.py records exceptions verbatim as ``{"error": str(exc)}``;
            # preserve the same shape so the grader's exception-handling
            # branch lines up.
            snapshot[key] = {"error": f"timeout after {timeout_s}s: {exc}"}
        except Exception as exc:  # noqa: BLE001 — best-effort capture
            snapshot[key] = {"error": str(exc)}

    # ---- Step 2: env_snapshot_files ----
    for pattern in getattr(task, "env_snapshot_files", None) or []:
        try:
            if "*" in pattern or "?" in pattern:
                # glob.glob is relative-path friendly when run with cwd, but we
                # want results keyed by the pattern's relative path; emulate
                # by changing directory via the search path argument.
                matches = sorted(
                    glob.glob(pattern, root_dir=str(work_dir), recursive=True)
                )[:_GLOB_MAX_FILES]
                for rel_path in matches:
                    abs_path = work_dir / rel_path
                    snapshot[f"file:{rel_path}"] = _read_file_entry(abs_path)
            else:
                abs_path = work_dir / pattern
                snapshot[f"file:{pattern}"] = _read_file_entry(abs_path)
        except Exception as exc:  # noqa: BLE001
            snapshot[f"file:{pattern}"] = {"error": str(exc)}

    # ---- Step 3: local_grader_files (host-side ground truth) ----
    for rel_path in getattr(task, "local_grader_files", None) or []:
        abs_path = task_dir / rel_path
        entry = _read_file_entry(abs_path)
        snapshot[f"local_file:{rel_path}"] = entry

    return snapshot


def _read_file_entry(abs_path: Path) -> dict[str, Any]:
    """Read a file from disk into the snapshot ``{content, encoding, mime_type}`` shape.

    Files are always base64-encoded so the schema is stable across text and
    binary. The container path emits the same triple via ``_normalize_read_response``
    (cli.py:204-220) for image files; we mirror it for all files to keep
    downstream graders' decode paths uniform.
    """
    if not abs_path.exists():
        return {"error": f"not found: {abs_path}"}
    if not abs_path.is_file():
        return {"error": f"not a regular file: {abs_path}"}
    try:
        raw = abs_path.read_bytes()
    except OSError as exc:
        return {"error": str(exc)}

    mime, _ = mimetypes.guess_type(str(abs_path))
    if mime is None:
        # Default to text/plain for unknown extensions; the container path
        # picks up the same fallback inside SandboxServer.read.
        mime = "text/plain"
    return {
        "content": base64.b64encode(raw).decode("ascii"),
        "encoding": "base64",
        "mime_type": mime,
    }
