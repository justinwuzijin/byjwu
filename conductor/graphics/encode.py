"""Alpha ``.mov`` files from RGBA frames, through ffmpeg.

ProRes 4444 (``prores_ks``, 10-bit 4:4:4 with a 16-bit alpha plane) works
wherever ffmpeg does and Final Cut reads it natively. HEVC with alpha needs
``hevc_videotoolbox``, which only exists on macOS; asking for it elsewhere
falls back to ProRes with a note. Output is bit-exact for the same frames.

``CONDUCTOR_FFMPEG`` names the binary (``off`` disables rendering).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

import numpy as np

from .render import RenderUnavailable

PRORES = [
    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
    "-alpha_bits", "16", "-vendor", "apl0",
    "-vf", "scale=out_color_matrix=bt709:out_range=tv",
    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
]
HEVC_ALPHA = ["-c:v", "hevc_videotoolbox", "-alpha_quality", "0.75", "-tag:v", "hvc1", "-pix_fmt", "bgra"]


@dataclass(frozen=True)
class Encoded:
    path: Path
    codec: str
    frames: int
    note: str | None = None


def find_ffmpeg() -> str | None:
    name = os.environ.get("CONDUCTOR_FFMPEG", "ffmpeg").strip() or "ffmpeg"
    if name.lower() in {"off", "0", "none"}:
        return None
    return shutil.which(name)


@lru_cache(maxsize=4)
def encoders(ffmpeg: str) -> frozenset[str]:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] == "V":
            names.add(parts[1])
    return frozenset(names)


def choose_codec(ffmpeg: str, wanted: str) -> tuple[str, str | None]:
    found = encoders(ffmpeg)
    if wanted == "hevc_alpha":
        if "hevc_videotoolbox" in found:
            return "hevc_alpha", None
        wanted = "prores4444"
        note = "HEVC with alpha needs macOS VideoToolbox, so the graphics were written as ProRes 4444."
    else:
        note = None
    if "prores_ks" not in found:
        raise RenderUnavailable(f"this ffmpeg has no prores_ks encoder, so no alpha .mov could be written ({ffmpeg})")
    return wanted, note


def encode(
    frames: Iterable[np.ndarray],
    out: Path,
    *,
    width: int,
    height: int,
    frame_duration: Fraction,
    codec: str = "prores4444",
    ffmpeg: str | None = None,
) -> Encoded:
    tool = ffmpeg or find_ffmpeg()
    if tool is None:
        raise RenderUnavailable("ffmpeg is not installed here, so no graphics .mov was rendered")
    used, note = choose_codec(tool, codec)
    rate = 1 / Fraction(frame_duration)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".part.mov")
    command = [
        tool, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}",
        "-framerate", f"{rate.numerator}/{rate.denominator}", "-i", "pipe:0", "-an",
        *(HEVC_ALPHA if used == "hevc_alpha" else PRORES),
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
        "-f", "mov", str(partial),
    ]
    count = 0
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RenderUnavailable(f"ffmpeg did not start: {exc}") from exc
    try:
        for frame in frames:
            if frame.shape != (height, width, 4) or frame.dtype != np.uint8:
                raise ValueError(f"frame {count} is {frame.shape} {frame.dtype}, expected ({height}, {width}, 4) uint8")
            process.stdin.write(np.ascontiguousarray(frame).tobytes())
            count += 1
        process.stdin.close()
        error = process.stderr.read().decode("utf-8", "replace").strip()
        code = process.wait(timeout=600)
    except BrokenPipeError:
        error = process.stderr.read().decode("utf-8", "replace").strip()
        code = process.wait(timeout=60)
    except Exception:
        process.kill()
        process.wait()
        partial.unlink(missing_ok=True)
        raise
    if code != 0 or count == 0:
        partial.unlink(missing_ok=True)
        raise RenderUnavailable(f"ffmpeg could not write {out.name}: {error.splitlines()[-1] if error else f'exit {code}'}")
    partial.replace(out)
    return Encoded(out, used, count, note)
