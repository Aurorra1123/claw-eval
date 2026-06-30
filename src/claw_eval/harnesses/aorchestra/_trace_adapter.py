"""AOrchestra trajectory + bridge step_log → claw-eval trace JSONL translator.

Phase 4 Wave 4-C — see ``docs/superpowers/specs/2026-06-24-aorchestra-harness-design.md``
§3 (translation table) and §4.5-4.6 (failure handling).

Input sources
-------------

* ``trajectory_path`` — JSON file written by AOrchestra's runner. Schema
  follows ``aorchestra/runners/gaia_runner.py:_save_trajectory``:

  .. code-block:: text

      {
        "task_id": str,
        "main_model": str,
        "sub_models": [str, ...],
        "success": bool,
        "trajectory": [
          {
            "attempt": int,
            "action": str,                 # tool name (e.g. "ocr_extract_text",
                                           # "delegate_task", "complete")
            "params": dict,                # kwargs passed to the tool
            "result": Any,                 # tool return value; for delegate_task
                                           # this is a nested dict including "trace"
            "raw_response": str,           # raw LLM response that produced this
                                           # action (assistant message)
            ...
          },
          ...
        ],
        ...
      }

  AOrchestra's MainAgent doesn't emit "plain text" steps in the way OpenClaw
  does — every trajectory entry is a tool invocation, and ``raw_response``
  carries the LLM's reasoning/text before/around the JSON action. We emit that
  as the assistant TraceMessage's TextBlock.

  ``delegate_task`` results carry an inner ``trace`` list of step dicts in the
  same shape (action / params / result / raw_response). We recurse into those
  with ``agent_role="sub"``.

* ``step_log_path`` — JSONL written by ``ClawEvalAction.__call__``. Each line
  follows the canonical shape produced by
  ``src/claw_eval/harnesses/aorchestra/_bridge/actions.py::_step_log_record``:

  .. code-block:: text

      {
        "toolCallId": str,
        "agent_role": "main" | "sub" | "agent",
        "tool": str,
        "url": str,
        "method": str,
        "request": Any,
        "status": int,                # HTTP status; -1 on transport error
        "response": Any,
        "durationMs": int,
        "error": str | None,
      }

Partial-input tolerance
-----------------------

* ``trajectory_path=None`` or the file is missing/empty/malformed →
  emit ``TraceStart + TraceMessage(user prompt) + TraceEnd(failure_modes=["error"])``.
* ``step_log_path=None`` or the file is missing → skip ``ToolDispatch``
  generation but keep the rest of the translation.

is_error rule (§4.6)
--------------------

The ``ToolResultBlock`` matching a trajectory tool call is marked
``is_error=True`` when:

1. the paired step_log record has ``status >= 400`` or ``status == -1``
   (transport error), OR
2. the paired step_log record's ``error`` field is non-null.

If no step_log record matches the trajectory toolCallId, we currently treat
the result as not-an-error — AOrchestra owns both data sources so a missing
record is a bug we don't want to silently amplify, but we still want graders
to be able to read the trace.

Assumption (documented inline because the spec leaves it open): each
trajectory step represents one LLM turn; we generate ONE assistant
TraceMessage per step plus, when the step is a tool invocation, the
corresponding ToolUseBlock + ToolResultBlock pair. The terminal ``complete``
action is treated as an assistant text turn (no ToolUseBlock) so the final
answer surfaces as plain assistant text to graders.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ...config import MediaConfig
from ...models.content import (
    AudioBlock,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    VideoBlock,
)
from ...models.message import Message
from ...models.trace import (
    AuditSnapshot,
    MediaLoad,
    TokenUsage,
    ToolDispatch,
    TraceEnd,
    TraceMessage,
    TraceStart,
)
from ...trace.writer import TraceWriter

if TYPE_CHECKING:
    from ...models.task import TaskDefinition


_log = logging.getLogger(__name__)


# Actions that are control-flow / terminal rather than user-visible tool
# calls. ``delegate_task`` is special-cased to recurse into the sub-trace.
# ``complete`` is the terminal answer action — we surface its params as
# assistant text so graders can read the final answer.
_TERMINAL_ACTIONS: frozenset[str] = frozenset({"complete"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_load_json(path: Path | None) -> dict[str, Any] | None:
    """Return the parsed trajectory JSON, or ``None`` for partial-input paths.

    A missing file, a malformed file, an empty file, or ``{}`` all degrade to
    ``None`` — the caller treats that as the partial-input path per §4.5.
    """
    if path is None:
        return None
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    try:
        raw = p.read_text()
    except OSError as exc:
        _log.warning("could not read trajectory %s: %s", p, exc)
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("malformed trajectory %s: %s", p, exc)
        return None
    if not isinstance(data, dict) or not data:
        # ``{}`` or non-dict counts as empty for partial-input purposes.
        return None
    if not data.get("trajectory"):
        # Trajectory file present but the list is missing/empty — still
        # treated as a partial input (no agent steps to translate).
        return None
    return data


def _load_step_log(path: Path | None) -> list[dict[str, Any]]:
    """Read the bridge step_log JSONL into a list of dicts.

    Returns ``[]`` for missing / unreadable / empty files. Malformed lines
    are skipped with a warning rather than aborting the translation.
    """
    if path is None:
        return []
    p = Path(path)
    if not p.exists() or not p.is_file():
        return []
    records: list[dict[str, Any]] = []
    with p.open() as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                _log.warning(
                    "step_log %s line %d: malformed JSON (%s) — skipped",
                    p, lineno, exc,
                )
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def _index_step_log(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Key the step_log records by ``toolCallId`` for O(1) lookup."""
    index: dict[str, dict[str, Any]] = {}
    for rec in records:
        tcid = rec.get("toolCallId")
        if isinstance(tcid, str) and tcid:
            index[tcid] = rec
    return index


def _extract_tool_call_id(result: Any) -> str | None:
    """Pull ``toolCallId`` out of a trajectory step's ``result``, if present.

    AOrchestra's ``ClawEvalAction.__call__`` (Wave 4-B actions.py) generates
    the id and embeds it in the tool's response payload alongside whatever
    the underlying mock service returned. We accept dict-shaped results
    only — anything else returns None and the call won't link to step_log.
    """
    if isinstance(result, dict):
        v = result.get("toolCallId")
        if isinstance(v, str) and v:
            return v
    return None


def _compute_is_error(
    step_log_rec: dict[str, Any] | None,
) -> bool:
    """Apply the §4.6 ``is_error`` rule.

    Truthy iff the paired step_log record has ``status >= 400`` or
    ``status == -1`` (transport error), OR ``error`` is non-null.
    """
    if step_log_rec is None:
        return False
    status = step_log_rec.get("status")
    if isinstance(status, int) and (status >= 400 or status == -1):
        return True
    if step_log_rec.get("error"):
        return True
    return False


def _stringify(value: Any) -> str:
    """Render a tool result as a single text string for ToolResultBlock."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _ensure_input_dict(params: Any) -> dict[str, Any]:
    """Tool inputs in claw-eval are dict-shaped; coerce defensively."""
    if isinstance(params, dict):
        return params
    return {}


_VALID_MODALITIES: frozenset[str] = frozenset({"image", "audio", "video", "document"})
_VALID_MEDIA_STATUSES: frozenset[str] = frozenset({"loaded", "skipped", "error"})


def _raw_load_for_media(path: Path | None) -> dict[str, Any] | None:
    """Tolerant trajectory read used only to recover the ``media`` list.

    ``_safe_load_json`` degrades a trajectory with an empty ``trajectory`` list
    to ``None`` (no agent steps). But images may have loaded before the agent
    errored, so the ``media`` records still matter for grading. This parse skips
    the empty-``trajectory`` gate and returns the dict (or ``None``).
    """
    if path is None:
        return None
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    try:
        raw = p.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _build_media_events(
    trajectory_data: dict[str, Any] | None,
    *,
    trace_id: str,
) -> list[MediaLoad]:
    """Translate the trajectory's ``media`` records into MediaLoad events.

    The AO runner (``_runner.py::_load_task_images``) records one dict per
    attachment with the same fields the native loop's MediaLoad carries
    (modality / source_path / mime_type / size_bytes / sha256 / status / note).
    The multimodal grader's ``image_loaded`` dimension keys off a
    ``MediaLoad(modality="image", status="loaded")`` event in the trace, so we
    surface them here. Malformed / out-of-range records are skipped defensively.
    """
    if not isinstance(trajectory_data, dict):
        return []
    raw = trajectory_data.get("media")
    if not isinstance(raw, list):
        return []
    events: list[MediaLoad] = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        modality = rec.get("modality")
        if modality not in _VALID_MODALITIES:
            continue
        status = rec.get("status", "loaded")
        if status not in _VALID_MEDIA_STATUSES:
            status = "loaded"
        try:
            size_bytes = int(rec.get("size_bytes", 0) or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        events.append(
            MediaLoad(
                trace_id=trace_id,
                modality=modality,  # type: ignore[arg-type]
                source_path=str(rec.get("source_path") or ""),
                mime_type=str(rec.get("mime_type") or ""),
                size_bytes=size_bytes,
                sha256=str(rec.get("sha256") or ""),
                status=status,  # type: ignore[arg-type]
                note=str(rec.get("note") or ""),
            )
        )
    return events


def _build_initial_media_blocks(
    trajectory_data: dict[str, Any] | None,
) -> list[ImageBlock | AudioBlock | VideoBlock | TextBlock]:
    """Rebuild the opening user message's media content blocks.

    The native loop's initial user message is ``[TextBlock, ImageBlock, ...]``
    (``loop.py::_build_initial_user_content``): for every attachment the model
    supports, it appends ``to_content_block(load_media_from_ref(...))``. The AO
    trajectory only persists ``media`` *metadata* (source_path / mime_type /
    status / ...) — never the base64 payload — so to mirror native we reload
    each ``status=="loaded"`` record from its ``source_path`` and re-encode it
    via the same ``media_loader`` helpers native uses.

    Only ``loaded`` records produce a block; ``skipped`` / ``error`` records do
    not (native only appends a block when the load succeeds AND the model
    supports the modality). Each re-load is independently tolerant: if the file
    has since been deleted / moved / become unreadable we skip that block and
    leave the message as plain text rather than aborting the whole rebuild.
    """
    # Local imports keep the module's top-level import surface unchanged for
    # non-multimodal tasks and avoid a hard dependency cycle through runner.
    from ...runner.media_loader import (
        MediaRef,
        load_media_from_ref,
        to_content_block,
    )

    if not isinstance(trajectory_data, dict):
        return []
    raw = trajectory_data.get("media")
    if not isinstance(raw, list):
        return []

    cfg = MediaConfig()
    blocks: list[ImageBlock | AudioBlock | VideoBlock | TextBlock] = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        if rec.get("modality") != "image":
            # Mirror fix scope: only image blocks are rebuilt into the opening
            # message (matches the T057-style multimodal alignment target).
            continue
        if rec.get("status") != "loaded":
            continue
        source_path = rec.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            continue
        mime_type = rec.get("mime_type")
        ref = MediaRef(
            raw_path=source_path,
            source="aorchestra_trajectory_media",
            mime_type=mime_type if isinstance(mime_type, str) and mime_type else None,
        )
        try:
            loaded = load_media_from_ref(
                ref,
                workspace_root=Path.cwd(),
                task_dir=None,
                max_bytes=cfg.max_bytes_per_file,
                image_max_dimension=cfg.image_max_dimension,
            )
            blocks.append(to_content_block(loaded))
        except Exception as exc:  # noqa: BLE001 — tolerate any reload failure
            _log.warning(
                "could not rebuild initial image block from %s: %s",
                source_path, exc,
            )
            continue
    return blocks


def _count_user_agent_rounds(trajectory_data: dict[str, Any] | None) -> int:
    """Number of ``user_agent_reply`` entries in the trajectory.

    The AO runner appends one such entry per simulated-user follow-up
    (``_runner.py``: ``"action": "user_agent_reply"``), so this matches the
    native loop's ``user_agent_rounds`` counter one-for-one.
    """
    if not isinstance(trajectory_data, dict):
        return 0
    traj = trajectory_data.get("trajectory")
    if not isinstance(traj, list):
        return 0
    return sum(
        1
        for s in traj
        if isinstance(s, dict) and s.get("action") == "user_agent_reply"
    )


# ---------------------------------------------------------------------------
# Trajectory walk
# ---------------------------------------------------------------------------


def _emit_step(
    step: dict[str, Any],
    *,
    trace_id: str,
    agent_role: Literal["main", "sub"],
    step_log_index: dict[str, dict[str, Any]],
    messages: list[TraceMessage],
) -> None:
    """Translate one trajectory step into TraceMessage events.

    Inline assumption: the LLM's response text lives in ``raw_response`` (we
    saw this in ``aorchestra/main_agent.py:226-231``). When ``raw_response``
    is missing we synthesise an empty assistant TextBlock so the ToolUseBlock
    still has a home.
    """
    action = step.get("action")
    params = _ensure_input_dict(step.get("params"))
    result = step.get("result")
    raw_response = step.get("raw_response")
    if not isinstance(raw_response, str):
        raw_response = ""

    # --- user_agent_reply: simulated user's follow-up turn ---
    # Emitted right after the assistant's ``complete`` for the same attempt, so
    # the conversation reads agent-answer → user-clarification in order. The
    # ``[user_agent]`` prefix is a HARD dependency of the user_agent_clarify
    # grader, which splits the trace into clarify/answer phases on the last
    # user message starting with that marker (user_agent_clarify_base.py).
    if action == "user_agent_reply":
        reply = ""
        if isinstance(result, dict):
            r = result.get("reply")
            if isinstance(r, str):
                reply = r
            elif r is not None:
                reply = _stringify(r)
        messages.append(
            TraceMessage(
                trace_id=trace_id,
                message=Message(
                    role="user",
                    content=[TextBlock(text=f"[user_agent]\n{reply}")],
                ),
                agent_role=agent_role,
            )
        )
        return

    # --- delegate_task: recurse into the sub-agent's trace ---
    if action == "delegate_task":
        # First emit the MainAgent's reasoning / decision as an assistant
        # message so the orchestration layer remains visible to graders.
        if raw_response:
            messages.append(
                TraceMessage(
                    trace_id=trace_id,
                    message=Message(
                        role="assistant",
                        content=[TextBlock(text=raw_response)],
                    ),
                    agent_role=agent_role,
                )
            )
        # Then walk the inner trace as SubAgent steps.
        if isinstance(result, dict):
            inner_trace = result.get("trace")
            if isinstance(inner_trace, list):
                for sub_step in inner_trace:
                    if isinstance(sub_step, dict):
                        _emit_step(
                            sub_step,
                            trace_id=trace_id,
                            agent_role="sub",
                            step_log_index=step_log_index,
                            messages=messages,
                        )
        return

    # --- complete: terminal answer surfaces as assistant text only ---
    if isinstance(action, str) and action in _TERMINAL_ACTIONS:
        # Combine raw_response with the answer text so graders can find it.
        answer = ""
        if isinstance(params, dict):
            ans_val = params.get("answer")
            if isinstance(ans_val, str):
                answer = ans_val
            elif ans_val is not None:
                answer = _stringify(ans_val)
        final_text = raw_response or answer
        if answer and answer not in final_text:
            final_text = f"{final_text}\n\n{answer}" if final_text else answer
        messages.append(
            TraceMessage(
                trace_id=trace_id,
                message=Message(
                    role="assistant",
                    content=[TextBlock(text=final_text)],
                ),
                agent_role=agent_role,
            )
        )
        return

    # --- regular tool call ---
    # Build assistant message: raw_response text + ToolUseBlock
    tool_call_id = _extract_tool_call_id(result) or uuid.uuid4().hex
    tool_name = action if isinstance(action, str) else "unknown_tool"

    assistant_content: list = []
    if raw_response:
        assistant_content.append(TextBlock(text=raw_response))
    assistant_content.append(
        ToolUseBlock(id=tool_call_id, name=tool_name, input=params)
    )
    messages.append(
        TraceMessage(
            trace_id=trace_id,
            message=Message(role="assistant", content=assistant_content),
            agent_role=agent_role,
        )
    )

    # Build user-role tool result
    step_log_rec = step_log_index.get(tool_call_id)
    is_error = _compute_is_error(step_log_rec)
    result_text = _stringify(result)
    messages.append(
        TraceMessage(
            trace_id=trace_id,
            message=Message(
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id=tool_call_id,
                        content=[TextBlock(text=result_text)],
                        is_error=is_error,
                    )
                ],
            ),
            agent_role=agent_role,
        )
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def translate_aorchestra(
    *,
    trajectory_path: Path | None,
    step_log_path: Path | None,
    audit_data: dict[str, dict],
    task: "TaskDefinition",
    run_id: str,
    trace_dir: Path,
    duration_ms: int,
    status: Literal["ok", "error", "timeout"],
) -> Path:
    """Translate an AOrchestra trajectory + step_log into a claw-eval trace.

    See module docstring for the full input contract and partial-input
    tolerance rules. Returns the absolute path to the written JSONL.
    """
    trace_dir = Path(trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{task.task_id}_{run_id}.jsonl"

    # Stable trace_id for the whole stream.
    trace_id = f"{task.task_id}_{run_id}_{uuid.uuid4().hex[:8]}"

    trajectory_data = _safe_load_json(trajectory_path)
    step_log_records = _load_step_log(step_log_path)
    step_log_index = _index_step_log(step_log_records)

    # Media records live in the trajectory JSON but must survive the
    # empty-``trajectory`` gate in ``_safe_load_json`` (images can be loaded
    # even if the agent then errored before emitting a step). Read them from a
    # tolerant raw parse so a multimodal MediaLoad event still reaches the
    # grader's ``image_loaded`` dimension.
    media_events = _build_media_events(
        trajectory_data if trajectory_data is not None
        else _raw_load_for_media(trajectory_path),
        trace_id=trace_id,
    )

    # Pull the model id from trajectory metadata; fall back to "" on partial.
    model = ""
    if trajectory_data is not None:
        main_model = trajectory_data.get("main_model")
        if isinstance(main_model, str):
            model = main_model

    # ----- compute failure_modes / wall-time per §4.5 -----
    failure_modes: list[str] = []
    if status == "timeout":
        failure_modes.append("timeout")
    elif status == "error":
        # Mirror OpenClaw shape — at minimum we mark "error"; richer
        # per-step error strings are appended below as we walk the trajectory.
        failure_modes.append("error")
    # Partial-input trajectory is also an error for grading purposes.
    if trajectory_data is None and status == "ok":
        failure_modes.append("error")

    wall_time_s = float(duration_ms) / 1000.0 if duration_ms else 0.0

    # ----- build message list -----
    messages: list[TraceMessage] = []

    # Open: user prompt. When the task loaded images, mirror the native loop's
    # initial user message shape (``[TextBlock, ImageBlock, ...]``) by reloading
    # the loaded media from disk and appending the rebuilt blocks. Non-multimodal
    # tasks (no loaded media) keep a pure-text opening message.
    opening_content: list = [TextBlock(text=task.prompt.text)]
    opening_content.extend(
        _build_initial_media_blocks(
            trajectory_data if trajectory_data is not None
            else _raw_load_for_media(trajectory_path)
        )
    )
    messages.append(
        TraceMessage(
            trace_id=trace_id,
            message=Message(
                role="user",
                content=opening_content,
            ),
            agent_role="main",
        )
    )

    assistant_turn_count = 0

    if trajectory_data is not None:
        trajectory_list = trajectory_data.get("trajectory") or []
        if not isinstance(trajectory_list, list):
            trajectory_list = []
        for step in trajectory_list:
            if not isinstance(step, dict):
                continue
            before = len(messages)
            _emit_step(
                step,
                trace_id=trace_id,
                agent_role="main",
                step_log_index=step_log_index,
                messages=messages,
            )
            # Roughly one assistant turn per emitted step (delegate_task may
            # emit many; we count post-hoc by inspecting role).
            for msg in messages[before:]:
                if msg.message.role == "assistant":
                    assistant_turn_count += 1

            # Per-step error surfacing into failure_modes (§4.5 row 3).
            result = step.get("result")
            if isinstance(result, dict):
                err_msg = result.get("error")
                if isinstance(err_msg, str) and err_msg and err_msg not in failure_modes:
                    failure_modes.append(err_msg)

    # ----- build ToolDispatch list -----
    dispatches: list[ToolDispatch] = []
    for rec in step_log_records:
        tcid = rec.get("toolCallId")
        tool_name = rec.get("tool")
        if not isinstance(tcid, str) or not isinstance(tool_name, str):
            continue
        status_code_raw = rec.get("status")
        # Normalise transport-error sentinel (-1) → 599 so the int field stays
        # meaningful and graders' status>=400 checks fire.
        if isinstance(status_code_raw, int):
            status_code = 599 if status_code_raw == -1 else status_code_raw
        else:
            status_code = 500
        req_body = rec.get("request") if isinstance(rec.get("request"), dict) else {}
        agent_role_val = rec.get("agent_role")
        if agent_role_val not in ("main", "sub", "agent"):
            agent_role_val = "agent"
        dispatches.append(
            ToolDispatch(
                trace_id=trace_id,
                tool_use_id=tcid,
                tool_name=tool_name,
                endpoint_url=str(rec.get("url") or ""),
                request_body=req_body,
                response_status=status_code,
                response_body=rec.get("response"),
                latency_ms=float(rec.get("durationMs") or 0),
                agent_role=agent_role_val,
            )
        )

    # ----- reconstruct user_agent stats for TraceEnd (mirror native loop) -----
    # native (``loop.py:567-569``) writes user_agent_rounds / _max_rounds /
    # _done. We rebuild them from the trajectory so the AO arm's TraceEnd
    # carries the same metadata. Non-user_agent tasks → 0 / 0 / False, matching
    # native's defaults for a task without a simulated user.
    user_agent_rounds = _count_user_agent_rounds(trajectory_data)
    ua_cfg = getattr(task, "user_agent", None)
    # Native reports max_rounds only when the user agent is enabled, else 0
    # (``loop.py``: ``ua_max_rounds = ua_cfg.max_rounds if ua_enabled else 0``).
    # Mirror that gate so a disabled-user_agent task reports 0, not the config
    # default. We can't see ``user_agent is not None`` here, but the trajectory's
    # presence of user_agent_reply rounds is the observable proxy: a disabled UA
    # never produces rounds.
    ua_enabled = bool(getattr(ua_cfg, "enabled", False))
    if ua_enabled:
        try:
            user_agent_max_rounds = int(getattr(ua_cfg, "max_rounds", 0) or 0)
        except (TypeError, ValueError):
            user_agent_max_rounds = 0
    else:
        user_agent_max_rounds = 0
    # ``user_agent_done`` in native is True ONLY when the simulated user returned
    # ``[DONE]`` (``generate_response`` → None) while rounds were not yet
    # exhausted; round-exhaustion ends the loop with done=False. The trajectory
    # has no explicit DONE marker, so we reconstruct conservatively: a run that
    # had user_agent rounds, succeeded, and did NOT hit the round cap can only
    # have terminated via [DONE] (the AO runner's user_agent loop breaks with
    # success on either [DONE] or cap-exhaustion — and cap-exhaustion implies
    # rounds == max_rounds). When rounds hit the cap, or the run failed, or
    # there were no user_agent rounds, we report False. Prefer a conservative
    # False over a false-positive True.
    run_succeeded = bool(trajectory_data.get("success")) if isinstance(trajectory_data, dict) else False
    user_agent_done = (
        user_agent_rounds > 0
        and run_succeeded
        and (user_agent_max_rounds == 0 or user_agent_rounds < user_agent_max_rounds)
    )

    # ----- write the trace -----
    with TraceWriter(trace_path) as writer:
        writer.write_event(
            TraceStart(
                trace_id=trace_id,
                task_id=task.task_id,
                model=model,
                harness="aorchestra",
            )
        )

        # Multimodal MediaLoad events (mirrors native loop, emitted right after
        # TraceStart so the grader's image_loaded dimension can find them).
        for media_event in media_events:
            writer.write_event(media_event)

        for msg in messages:
            writer.write_event(msg)

        for disp in dispatches:
            writer.write_event(disp)

        for svc_name, svc_data in (audit_data or {}).items():
            writer.write_event(
                AuditSnapshot(
                    trace_id=trace_id,
                    service_name=svc_name,
                    audit_url=f"http://aorchestra-placeholder/{svc_name}/audit",
                    audit_data=svc_data if isinstance(svc_data, dict) else {},
                )
            )

        # Token totals — AOrchestra's trajectory doesn't expose per-call usage
        # at the runner level (cost is dollars, not tokens). Leave them zero;
        # graders that need tokens fall back to dispatch counts.
        writer.write_event(
            TraceEnd(
                trace_id=trace_id,
                total_turns=assistant_turn_count,
                model_input_tokens=0,
                model_output_tokens=0,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                model_time_s=0.0,
                tool_time_s=0.0,
                other_time_s=0.0,
                wall_time_s=wall_time_s,
                failure_modes=failure_modes,
                user_agent_rounds=user_agent_rounds,
                user_agent_max_rounds=user_agent_max_rounds,
                user_agent_done=user_agent_done,
            )
        )

    return trace_path
