"""Synthetic shoot through room-run, then the style score and the linters.

Marked slow: it generates media with ffmpeg (or placeholders) and runs the
dry-run assembler. No API key. No real footage.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generate = _load("synth_e2e_shoot", ROOT / "scripts" / "synth_e2e_shoot.py").generate
score_timeline = _load("style_score", ROOT / "eval" / "style_score.py").score_timeline

pytestmark = pytest.mark.slow

BRIEF = "A short diary about why an edit feels slow, and the one rule that fixes it."


def test_synthetic_room_run_matches_the_style(tmp_path):
    shoot = tmp_path / "shoot"
    info = generate(shoot)
    assert info["clips"] >= 8
    assert (shoot / "bed_120bpm.wav").is_file()
    assert (shoot / "bed_96bpm.wav").is_file()
    assert not any(path.suffix == ".mp4" and path.stat().st_size > 5_000_000 for path in shoot.glob("*.mp4"))

    from conductor.room import room_run

    result = room_run(shoot, out_root=tmp_path / "out", brief=BRIEF, style="byjustinwu")
    assert result.ok
    assert result.payload["mode"] == "dry-run"
    assert result.payload["flow"] == "assemble+iterate"
    opened = Path(result.payload["open_in_final_cut"])
    assert opened.is_file()
    assembly = result.out_dir / "assemble" / "assembly.json"
    report = score_timeline(opened, style="byjustinwu", assembly=assembly)
    (result.out_dir / "style-score.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert report["lint"]["passed"], report["lint"]["hard"]
    assert report["critic"]["passed"], report["critic"]
    assert report["overall"] >= 88, report["dimensions"]
    assert min(report["dimensions"].values()) >= 80, report["dimensions"]


def test_no_sidecar_room_run_matches_the_style(tmp_path):
    espeak = shutil.which("espeak-ng")
    if espeak is None:
        pytest.skip("espeak-ng is not installed")
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        pytest.skip("faster-whisper is not installed")
    from conductor.signals import _faster_model_name

    if _faster_model_name() is None:
        pytest.skip("no local whisper model and base.en could not be resolved")

    shoot = tmp_path / "shoot"
    info = generate(shoot, sidecars=False)
    assert info["tts"] is True
    assert not list(shoot.glob("*.srt"))
    assert not list(shoot.glob("*.json")) or {path.name for path in shoot.glob("*.json")} <= {"durations.json"}

    from conductor.room import room_run

    result = room_run(shoot, out_root=tmp_path / "out", brief=BRIEF, style="byjustinwu")
    assert result.ok
    opened = Path(result.payload["open_in_final_cut"])
    assembly = result.out_dir / "assemble" / "assembly.json"
    report = score_timeline(opened, style="byjustinwu", assembly=assembly)
    (result.out_dir / "style-score.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert report["lint"]["passed"], report["lint"]["hard"]
    assert report["critic"]["passed"], report["critic"]
    assert report["overall"] >= 88, report["dimensions"]
    assert min(report["dimensions"].values()) >= 80, report["dimensions"]


def test_one_short_song_room_run_matches_the_style(tmp_path):
    espeak = shutil.which("espeak-ng")
    if espeak is None:
        pytest.skip("espeak-ng is not installed")
    shoot = tmp_path / "shoot"
    info = generate(shoot, sidecars=False, beds="short")
    assert info["songs"] == [{"name": "bed_120bpm.wav", "bpm": 120.0, "seconds": 48.0}]
    assert info["tts"] is True

    from conductor.room import room_run

    result = room_run(shoot, out_root=tmp_path / "out", brief=BRIEF, style="byjustinwu")
    assert result.ok
    if result.payload["cuts_applied"] == 0:
        assert "nothing further to trim" in result.markdown
    else:
        assert f"Cuts applied: {result.payload['cuts_applied']}" in result.markdown
    opened = Path(result.payload["open_in_final_cut"])
    assembly = result.out_dir / "assemble" / "assembly.json"
    report = score_timeline(opened, style="byjustinwu", assembly=assembly)
    (result.out_dir / "style-score.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert report["lint"]["passed"], report["lint"]["hard"]
    assert report["critic"]["passed"], report["critic"]
    assert report["overall"] >= 88, report["dimensions"]
    assert min(report["dimensions"].values()) >= 80, report["dimensions"]
