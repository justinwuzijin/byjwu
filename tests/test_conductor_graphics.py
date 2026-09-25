"""Subtitles, distorted titles, and the rectangle layer on an FCPXML."""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path

from conductor.fcpxml import local, parse_fcpxml
from conductor.graphics import apply_graphics
from conductor.graphics.diffusion import group_by, sample_keyframes
from conductor.graphics.profile import from_mapping
from conductor.graphics.render import rect_frame, rect_schedule
from conductor.graphics.stage import BASIC_TITLE_UID
from conductor.router import Router
from conductor.timing import anchor_time

FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="interview" start="0s" duration="30s" hasVideo="1" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="file:///tmp/interview.mov"/>
    </asset>
  </resources>
  <library>
    <event name="Day">
      <project name="Cut">
        <sequence format="r1" duration="8s" tcStart="0s" tcFormat="NDF">
          <spine>
            <asset-clip ref="r2" offset="0s" name="Open" start="0s" duration="4s"/>
            <asset-clip ref="r2" offset="4s" name="Next" start="4s" duration="4s"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""

WORDS = {
    "protocol": "cut-conductor.words",
    "protocol_version": 1,
    "words": [
        {"text": "waterloo", "start": "1s", "end": "3/2s", "sequence": "Cut", "partial": False},
        {"text": "engineering", "start": "3/2s", "end": "2s", "sequence": "Cut", "partial": False},
        {"text": "is", "start": "2s", "end": "9/4s", "sequence": "Cut", "partial": False},
        {"text": "hard", "start": "9/4s", "end": "5/2s", "sequence": "Cut", "partial": False},
        {"text": "co-op", "start": "7/2s", "end": "4s", "sequence": "Cut", "partial": True,
         "file_start_seconds": 3.5, "file_end_seconds": 4.5},
        {"text": "term", "start": "5s", "end": "11/2s", "sequence": "Cut", "partial": False},
        {"text": "starts", "start": "11/2s", "end": "6s", "sequence": "Cut", "partial": False},
    ],
}


def _profile(**rect):
    layer = {"render_scale": 0.05, "coverage": "titles", "density": 2, "beat_sync": False, "palette": ["#FFFFFF"]}
    layer.update(rect)
    return from_mapping(
        {
            "graphics": {
                "enabled": True,
                "text_fx": {"duration_seconds": 0.5, "size": 64, "max_titles": 4},
                "rect_layer": layer,
                "subtitles": {"max_chars_per_line": 16, "max_lines": 2, "min_seconds": 0.5, "max_seconds": 3},
            }
        },
        source="test",
    )


def _ranges(path: Path):
    document = parse_fcpxml(path)
    sequence = document.sequences[0]
    rows = []
    for clip in sequence.spine:
        for child in clip.element:
            if local(child.tag) not in {"title", "asset-clip"}:
                continue
            if child.get("name") not in {"Subtitle"} and not (child.get("name") or "").startswith("byjwu"):
                continue
            offset = Fraction(child.get("offset").replace("s", "")) if "/" not in child.get("offset") else _frac(child.get("offset"))
            start = anchor_time(clip.element, clip.timeline_start, offset, sequence.frame_duration)
            duration = _frac(child.get("duration"))
            rows.append((child.get("lane"), child.get("name"), start, start + duration, clip.timeline_start, clip.timeline_end))
    return rows


def _frac(value: str) -> Fraction:
    body = value[:-1]
    if "/" in body:
        num, den = body.split("/")
        return Fraction(int(num), int(den))
    return Fraction(body)


def test_subtitles_stay_inside_one_clip_and_do_not_overlap(tmp_path):
    source = tmp_path / "cut.fcpxml"
    source.write_text(FIXTURE, encoding="utf-8")
    out = tmp_path / "cut.graphics.fcpxml"
    with Router(live=False) as router:
        result = apply_graphics(source, out_path=out, words=WORDS, profile=_profile(), router=router, enabled=True)
    assert result.fcpxml == out
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "Basic Title" in text
    assert BASIC_TITLE_UID in text
    assert 'font="SF Pro Text"' in text
    rows = [row for row in _ranges(out) if row[1] == "Subtitle"]
    assert rows
    for _lane, _name, start, end, clip_start, clip_end in rows:
        assert clip_start <= start < end <= clip_end
    rows.sort(key=lambda row: row[2])
    for earlier, later in zip(rows, rows[1:]):
        assert earlier[3] <= later[2]
    assert (tmp_path / "cut.graphics.assets").is_dir()


def test_rectangle_layer_is_deterministic():
    profile = _profile(coverage="full", beat_sync=True)
    beats = [0.5, 1.0]
    seed = 7
    first = rect_schedule(profile.rect_layer, 2.0, seed, beats)
    second = rect_schedule(profile.rect_layer, 2.0, seed, beats)
    assert first == second
    frame_a = rect_frame(first, profile.rect_layer, 0.25, 3, 32, 18, seed, duration=2.0, beats=beats)
    frame_b = rect_frame(second, profile.rect_layer, 0.25, 3, 32, 18, seed, duration=2.0, beats=beats)
    assert frame_a.tobytes() == frame_b.tobytes()
    other = rect_frame(first, profile.rect_layer, 0.25, 3, 32, 18, seed + 1, duration=2.0, beats=beats)
    assert frame_a.tobytes() != other.tobytes()


def test_missing_ffmpeg_skips_renders_and_keeps_subtitles(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_FFMPEG", "off")
    source = tmp_path / "cut.fcpxml"
    source.write_text(FIXTURE, encoding="utf-8")
    out = tmp_path / "cut.graphics.fcpxml"
    result = apply_graphics(source, out_path=out, words=WORDS, profile=_profile(), enabled=True)
    assert out.is_file()
    assert any("ffmpeg" in note for note in result.notes)
    assert 'name="Subtitle"' in out.read_text(encoding="utf-8")
    assert list(out.parent.glob("*.mov")) == []
    assert source.read_text(encoding="utf-8") == FIXTURE


def test_graphics_package_ships_no_private_material():
    """SF Pro is a font name. No font file, home path, or real export is committed."""
    root = Path("conductor/graphics")
    home = "/Users/" + "justinwu"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert home not in text
        assert "/Users/" not in text
    assert home not in FIXTURE
    assert "file:///tmp/interview.mov" in FIXTURE
    assert "*.assets/" in Path(".gitignore").read_text(encoding="utf-8")
    shipped = list(root.rglob("*.ttf")) + list(root.rglob("*.otf")) + list(root.rglob("*.ttc"))
    assert shipped == []


def test_caption_grouping_and_native_shapes(tmp_path):
    words = [
        type("W", (), {"text": "one", "start": 0.0, "end": 0.4})(),
        type("W", (), {"text": "two", "start": 0.4, "end": 0.8})(),
        type("W", (), {"text": "three", "start": 0.8, "end": 1.2})(),
    ]
    assert [[word.text for word in group] for group in group_by(words, length=6)] == [["one", "two"], ["three"]]
    assert sample_keyframes([(0.0, 1.0), (1.0, 3.0)], 0.5, "smooth") == 2.0
    source = tmp_path / "cut.fcpxml"
    source.write_text(FIXTURE, encoding="utf-8")
    profile = _profile(coverage="full")
    out = tmp_path / "native.fcpxml"
    apply_graphics(source, out_path=out, words=WORDS, profile=profile, enabled=True)
    text = out.read_text(encoding="utf-8")
    assert 'name="byjwu rectangles"' in text
    assert "keyframeAnimation" in text
    assert "Shapes" in text
    assert "Gaussian" in text


def test_graphics_stay_off_without_a_flag_or_profile(tmp_path):
    source = tmp_path / "cut.fcpxml"
    source.write_text(FIXTURE, encoding="utf-8")
    result = apply_graphics(source, out_path=tmp_path / "nope.fcpxml", words=WORDS, enabled=False)
    assert result.enabled is False
    assert not (tmp_path / "nope.fcpxml").exists()
    assert os.environ.get("CONDUCTOR_FFMPEG") != "off"
