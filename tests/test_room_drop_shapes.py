"""A clip folder with no FCPXML, the way a room drop arrives.

Mixed frame rates, a portrait clip, and a clip with no audio must still
produce an FCPXML. Dry-run, no API key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

KEYS = (
    "OPENROUTER_API_KEY",
    "TYPESAFE_API_KEY",
    "ANTHROPIC_API_KEY",
    "CURSOR_API_KEY",
    "CURSOR_AUTH_TOKEN",
    "XAI_API_KEY",
    "CONDUCTOR_TASTE_KEY",
)


def _clip(ffmpeg: str, dest: Path, source: str, seconds: str, *, audio: bool) -> None:
    command = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"{source},trim=duration={seconds}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}", "-c:a", "aac", "-shortest"]
    else:
        command += ["-an"]
    command += ["-t", seconds, "-c:v", "mpeg4", "-q:v", "12", "-pix_fmt", "yuv420p", str(dest)]
    completed = subprocess.run(command, capture_output=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr


def test_drop_folder_survives_mixed_shapes(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed")
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    assert not any(os.environ.get(key) for key in KEYS)

    folder = tmp_path / "drop"
    folder.mkdir()
    _clip(ffmpeg, folder / "a_23976.mp4", "testsrc2=size=320x180:rate=24000/1001", "3", audio=True)
    _clip(ffmpeg, folder / "b_2997.mov", "testsrc=size=320x180:rate=30000/1001", "3", audio=True)
    _clip(ffmpeg, folder / "c_5994.mp4", "smptebars=size=320x180:rate=60000/1001", "3", audio=True)
    _clip(ffmpeg, folder / "d_portrait.mp4", "testsrc2=size=180x320:rate=24", "3", audio=True)
    _clip(ffmpeg, folder / "e_silent.mp4", "mandelbrot=size=320x180:rate=24", "3", audio=False)
    from conductor.assembly.synth import write_click_track

    write_click_track(folder / "bed.wav", bpm=100, seconds=20)

    from conductor.room import room_run

    first = room_run(folder, out_root=tmp_path / "out", brief="A short test of mixed media.", style="byjustinwu")
    second = room_run(folder, out_root=tmp_path / "out", brief="A short test of mixed media.", style="byjustinwu")
    assert first.ok and second.ok
    assert first.payload["mode"] == "dry-run"
    assert first.payload["flow"] == "assemble+iterate"
    opened = Path(first.payload["open_in_final_cut"])
    assert opened.is_file()
    assert opened != Path(second.payload["open_in_final_cut"])
    assert opened.parent.parent.name != Path(second.payload["open_in_final_cut"]).parent.parent.name

    texts = [path.read_text(encoding="utf-8") for path in first.out_dir.rglob("*.fcpxml")]
    assert texts
    frames = set()
    portrait = False
    for text in texts:
        root = ET.fromstring(text)
        frames.update(fmt.get("frameDuration") for fmt in root.iter("format"))
        portrait = portrait or any(
            int(fmt.get("height") or 0) > int(fmt.get("width") or 0) for fmt in root.iter("format")
        )
    assert "1001/24000s" in frames
    assert "1001/30000s" in frames
    assert "1001/60000s" in frames
    assert portrait
    assembly = json.loads((first.out_dir / "assemble" / "assembly.json").read_text(encoding="utf-8"))
    silent = [row for row in assembly.get("units") or [] if "silent" in str(row.get("clip"))]
    assert silent and "no audio" in str(silent[0].get("basis"))
    opened_root = ET.parse(opened).getroot()
    assert opened_root.find(".//spine") is not None
