"""Sandbox frame compression / budget (OpenClaw-arm vision gap fix).

The native ``SandboxToolDispatcher`` resizes + JPEG-compresses media frames and
caps them to a per-turn budget. The sandbox server now exposes the same contract
as opt-in request fields (``frame_format`` / ``frame_quality`` / ``frame_budget``)
so the OpenClaw bridge can mirror it. These tests lock the encoder + subsample
behaviour; the default (PNG, no budget) path stays byte-for-byte unchanged so the
native arm is unaffected.
"""
from __future__ import annotations

import base64
import io
import shutil
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from claw_eval.sandbox.server import (  # noqa: E402
    ReadMediaRequest,
    _image_to_b64_jpeg,
    _image_to_b64_png,
    _read_video,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "tasks" / "M050_video_shopping_receipt" / "fixtures" / "shopping.mp4"


def test_jpeg_encoder_smaller_than_png_and_decodable() -> None:
    # Photo-like noise (flat colours would let PNG win); mirrors real frames.
    import os
    img = Image.frombytes("RGB", (640, 360), os.urandom(640 * 360 * 3))
    jpeg_b64 = _image_to_b64_jpeg(img, quality=60)
    png_b64 = _image_to_b64_png(img)
    jpeg_bytes = base64.b64decode(jpeg_b64)
    # valid JPEG (decodes, JPEG magic) and materially smaller than PNG.
    assert jpeg_bytes[:2] == b"\xff\xd8"
    assert Image.open(io.BytesIO(jpeg_bytes)).format == "JPEG"
    assert len(jpeg_bytes) < len(base64.b64decode(png_b64))


def test_jpeg_encoder_flattens_rgba() -> None:
    # RGBA must composite onto white (JPEG has no alpha) without raising.
    rgba = Image.new("RGBA", (64, 64), (10, 20, 30, 128))
    out = base64.b64decode(_image_to_b64_jpeg(rgba, quality=60))
    assert Image.open(io.BytesIO(out)).mode == "RGB"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
@pytest.mark.skipif(not _FIXTURE.exists(), reason="fixture video missing")
def test_read_video_jpeg_budget_is_smaller_and_capped() -> None:
    base = dict(path=str(_FIXTURE), fps=1, max_frames=30, start_time=0, end_time=80)

    # Default: full-resolution PNG, no budget (native arm path — unchanged).
    old = _read_video(_FIXTURE, ReadMediaRequest(**base))
    assert old["frames"][0]["mime_type"] == "image/png"

    # Bridge contract: downscaled JPEG + frame budget.
    new = _read_video(_FIXTURE, ReadMediaRequest(
        **{**base, "max_frames": 74, "end_time": 254,
           "screen_size": "1280x1280", "frame_format": "jpeg",
           "frame_quality": 60, "frame_budget": 64},
    ))
    assert new["frames"][0]["mime_type"] == "image/jpeg"
    # budget caps the count ...
    assert len(new["frames"]) <= 64
    # ... and each frame is dramatically smaller than the full-res PNG.
    old_avg = sum(len(base64.b64decode(f["image_b64"])) for f in old["frames"]) / len(old["frames"])
    new_avg = sum(len(base64.b64decode(f["image_b64"])) for f in new["frames"]) / len(new["frames"])
    assert new_avg < old_avg / 4  # observed ~15x; guard well below that


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
@pytest.mark.skipif(not _FIXTURE.exists(), reason="fixture video missing")
def test_read_video_default_path_unchanged_for_native() -> None:
    # No opt-in fields → identical shape to the pre-fix behaviour (PNG frames).
    res = _read_video(_FIXTURE, ReadMediaRequest(path=str(_FIXTURE), fps=1, max_frames=8))
    assert all(f["mime_type"] == "image/png" for f in res["frames"])
    assert len(res["frames"]) <= 8
