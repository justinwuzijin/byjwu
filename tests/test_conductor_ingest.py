"""Folder of clips → starter FCPXML → the same shadow and apply gates."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from conductor.cli import main
from conductor.errors import ConductorError
from conductor.fcpxml import parse_fcpxml
from conductor.ingest import (
    FALLBACK_DURATION,
    Probe,
    Skip,
    file_hash,
    file_url,
    ingest,
    inventory,
    load_duration_overrides,
    parse_ffprobe,
)

FIXTURE = Path("fixtures/selects")
DURATIONS = FIXTURE / "durations.json"
BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def _hashes(root: Path) -> dict[Path, str]:
    found = {}
    for path in root.rglob("*"):
        if path.is_file():
            found[path] = file_hash(path)
    return found


def _spine(path: Path):
    return parse_fcpxml(path).sequences[0].spine


def test_fixture_inventory_is_filename_order_and_skips_the_rest():
    found = inventory(FIXTURE, overrides=load_duration_overrides(DURATIONS))
    assert [clip.name for clip in found.clips] == [
        "a_interview.mp4",
        "b_flash.mov",
        "c_button.m4v",
    ]
    assert found.clips[1].duration == Fraction(1, 8)
    assert found.clips[1].duration_source == "override"
    assert "notes.txt" in found.skipped
    assert all(not clip.name.startswith(".") for clip in found.clips)
    assert all("ignored" not in clip.name for clip in found.clips)


def test_ingest_writes_absolute_urls_and_leaves_media_alone(tmp_path):
    before = _hashes(FIXTURE)
    result = ingest(
        FIXTURE,
        durations_path=DURATIONS,
        brief=BRIEF,
        out_dir=tmp_path,
    )
    assert _hashes(FIXTURE) == before
    assert result.report.mode == "dry-run"
    assert result.report.payload["applied"] is False
    assert result.report.out_applied is None
    document = parse_fcpxml(result.starter)
    spine = document.sequences[0].spine
    assert document.sequences[0].name == "selects"
    assert [clip.name for clip in spine] == ["a_interview", "b_flash", "c_button"]
    assert spine[0].offset == 0 and spine[0].duration == 8
    assert spine[1].offset == 8 and spine[1].duration == Fraction(1, 8)
    assert spine[2].offset == Fraction(65, 8) and spine[2].duration == 6
    assert document.sequences[0].duration == Fraction(113, 8)
    for clip in spine:
        src = document.assets[clip.ref].src
        parsed = urlparse(src)
        assert parsed.scheme == "file"
        assert parsed.netloc == ""
        media = Path(unquote(parsed.path))
        assert media.is_absolute() and media.is_file()
        assert src == file_url(media)
    ingest_info = result.report.payload["ingest"]
    assert ingest_info["order"] == "filename"
    assert Path(ingest_info["starter_fcpxml"]) == result.starter.resolve()
    text = result.report.out_markdown.read_text()
    assert "## Ingest" in text
    assert "Relink Files" in text
    again = ingest(FIXTURE, durations_path=DURATIONS, brief=BRIEF, out_dir=tmp_path)
    assert again.starter.read_bytes() == result.starter.read_bytes()


def test_apply_removes_only_the_flash_and_keeps_the_starter(tmp_path):
    before = _hashes(FIXTURE)
    result = ingest(
        FIXTURE,
        durations_path=DURATIONS,
        brief=BRIEF,
        out_dir=tmp_path,
        apply=True,
        min_confidence=0.8,
        passes=["mechanical"],
    )
    assert _hashes(FIXTURE) == before
    assert result.report.cuts_applied == 1
    assert [clip.name for clip in _spine(result.starter)] == [
        "a_interview",
        "b_flash",
        "c_button",
    ]
    assert [clip.name for clip in _spine(result.report.out_fcpxml)] == [
        "a_interview",
        "b_flash",
        "c_button",
    ]
    applied = _spine(result.report.out_applied)
    assert [clip.name for clip in applied] == ["a_interview", "c_button"]
    assert applied[0].duration == 8
    assert applied[1].duration == 6
    assert applied[1].offset == 8


def test_apply_without_a_gate_is_refused(tmp_path):
    with pytest.raises(ConductorError, match="--accept"):
        ingest(FIXTURE, durations_path=DURATIONS, out_dir=tmp_path, apply=True)


def test_creative_confidence_does_not_cut(tmp_path):
    with pytest.raises(ConductorError, match="Creative passes"):
        ingest(
            FIXTURE,
            durations_path=DURATIONS,
            out_dir=tmp_path,
            apply=True,
            min_confidence=0.8,
            passes=["dialogue"],
        )


def test_fallback_duration_when_probe_returns_none(tmp_path):
    media = tmp_path / "reel"
    media.mkdir()
    (media / "take.mp4").write_bytes(b"not a video")
    (media / "notes.txt").write_text("skip")
    result = ingest(media, out_dir=tmp_path / "out", probe=lambda path: None)
    clip = result.inventory.clips[0]
    assert clip.duration == FALLBACK_DURATION
    assert clip.duration_source == "fallback"
    assert clip.width == 1920 and clip.height == 1080
    assert "placeholder" in (clip.warning or "")
    assert result.report.payload["brief"].startswith("Assemble these clips")
    assert "notes.txt" in result.inventory.skipped


def test_override_does_not_call_the_probe(tmp_path, monkeypatch):
    media = tmp_path / "reel"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"a")
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({"a.mp4": 4, "missing.mov": "1s"}))

    def boom(path):
        raise AssertionError(f"probe called for {path}")

    monkeypatch.setattr("conductor.ingest.ffprobe_probe", boom)
    result = ingest(media, out_dir=tmp_path / "out", durations_path=durations)
    assert result.inventory.clips[0].duration_source == "override"
    assert result.inventory.clips[0].duration == 4
    assert any("missing.mov" in warning for warning in result.warnings)


def test_spaces_and_extra_format_round_trip(tmp_path):
    media = tmp_path / "my clips"
    media.mkdir()
    (media / "a b.mp4").write_bytes(b"a")
    (media / "c.MXF").write_bytes(b"c")
    nested = media / "nested"
    nested.mkdir()
    (nested / "nope.mp4").write_bytes(b"n")
    (media / ".hidden.mov").write_bytes(b"h")

    def probe(path: Path):
        if path.suffix.lower() == ".mxf":
            return Probe(Fraction(3), 1280, 720, Fraction(1, 30), True, False)
        return Probe(Fraction(2), 1920, 1080, Fraction(1, 24), True, True)

    found = inventory(media, probe=probe)
    assert [clip.name for clip in found.clips] == ["a b.mp4", "c.MXF"]
    assert "%20" in found.clips[0].src
    assert " " not in found.clips[0].src
    result = ingest(media, out_dir=tmp_path / "out", probe=probe, name="A&B")
    document = parse_fcpxml(result.starter)
    assert document.sequences[0].name == "A&B"
    assert result.starter.name == "A-B.fcpxml"
    first, second = document.sequences[0].spine
    assert first.name == "a b"
    assert document.assets[first.ref].has_audio
    assert second.name == "c"
    assert document.assets[second.ref].has_audio is False
    assert document.formats[document.sequences[0].format_id].width == 1920
    assert (media / "a b.mp4").read_bytes() == b"a"


def test_audio_only_probe_is_skipped(tmp_path):
    media = tmp_path / "reel"
    media.mkdir()
    (media / "song.mp4").write_bytes(b"s")
    (media / "picture.mov").write_bytes(b"p")

    def probe(path: Path):
        if path.suffix == ".mp4":
            return Skip("no video stream")
        return Probe(Fraction(5))

    found = inventory(media, probe=probe)
    assert [clip.name for clip in found.clips] == ["picture.mov"]
    assert any("song.mp4" in warning for warning in found.warnings)


def test_empty_and_missing_folders(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConductorError, match="no video files"):
        inventory(empty)
    with pytest.raises(ConductorError, match="no such media folder"):
        inventory(tmp_path / "missing")
    video = tmp_path / "one.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ConductorError, match="is a file"):
        inventory(video)


def test_parse_ffprobe_json():
    probed = parse_ffprobe(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "24/1",
                    "duration": "8.0",
                },
                {"codec_type": "audio"},
            ],
            "format": {"duration": "8.0"},
        }
    )
    assert isinstance(probed, Probe)
    assert probed.duration == 8
    assert probed.frame_duration == Fraction(1, 24)
    assert probed.has_audio
    assert isinstance(parse_ffprobe({"streams": [{"codec_type": "audio"}]}), Skip)
    assert parse_ffprobe({"streams": [], "format": {}}) is None


def test_bad_durations_file(tmp_path):
    bad = tmp_path / "durations.json"
    bad.write_text('{"a.mp4": "soon"}')
    with pytest.raises(ConductorError, match="FCP time"):
        load_duration_overrides(bad)
    bad.write_text('["a.mp4"]')
    with pytest.raises(ConductorError, match="object"):
        load_duration_overrides(bad)
    bad.write_text('{"nested/a.mp4": 1}')
    with pytest.raises(ConductorError, match="file names"):
        load_duration_overrides(bad)


def test_cli_fixture_and_refuses_apply_flags_without_apply(tmp_path, capsys):
    code = main(
        [
            "ingest",
            "--media",
            "fixtures/selects",
            "--durations",
            "fixtures/selects/durations.json",
            "--brief",
            BRIEF,
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "starter" in output and "shadow" in output
    assert (tmp_path / "selects.fcpxml").is_file()
    assert (tmp_path / "selects.conductor.fcpxml").is_file()
    refused = main(
        [
            "ingest",
            "--media",
            "fixtures/selects",
            "--accept",
            "c0001",
            "--out-dir",
            str(tmp_path / "nope"),
        ]
    )
    assert refused == 2
    assert "require --apply" in capsys.readouterr().err


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg not installed",
)
def test_ffprobe_reads_a_generated_clip(tmp_path):
    clip = tmp_path / "tiny.mp4"
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-shortest",
            "-t",
            "1",
            str(clip),
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("ffmpeg could not synthesize a clip")
    found = inventory(tmp_path)
    assert found.clips[0].duration_source == "ffprobe"
    assert found.clips[0].width == 320
    assert found.clips[0].height == 240
    assert abs(float(found.clips[0].duration) - 1) < 0.05
    assert found.clips[0].has_audio
