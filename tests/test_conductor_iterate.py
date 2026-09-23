"""The bot's iterate loop, and the drop folder it reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conductor.cli import main
from conductor.drop import DESKTOP_IN, DESKTOP_OUT, output_dir_for, resolve_drop
from conductor.errors import ConductorError
from conductor.fcpxml import parse_fcpxml
from conductor.iterate import format_table, iterate
from conductor.metrics import Targets
from conductor.taste import load_taste

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
SELECTS = Path("fixtures/selects")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."

_TINY = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r2" name="talk" uid="A-TALK" start="0s" duration="60s" hasVideo="1" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="file:///Volumes/Media/talk.mov"/>
    </asset>
  </resources>
  <library>
    <event name="Drop">
      <project name="Tiny">
        <sequence format="r1" duration="11s" tcStart="0s" tcFormat="NDF">
          <spine>
            <gap name="Gap" offset="0s" start="0s" duration="3s"/>
            <asset-clip ref="r2" offset="3s" name="Interview" start="0s" duration="8s"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


def _guest(path: Path):
    spine = parse_fcpxml(path).sequences[0].spine
    return next(clip for clip in spine if clip.name == "Guest explains")


def test_drop_folder_maps_jevid_in_to_jevid_out(tmp_path):
    assert DESKTOP_IN == Path.home() / "Desktop" / "jevid-in"
    assert DESKTOP_OUT == Path.home() / "Desktop" / "jevid-out"
    assert output_dir_for(Path("/tmp/jevid-in")) == Path("/tmp/jevid-out")
    assert output_dir_for(Path("/tmp/selects")) == Path("/tmp/selects-out")
    assert output_dir_for(Path("/tmp/jevid-in"), tmp_path / "explicit") == tmp_path / "explicit"

    folder = tmp_path / "jevid-in"
    folder.mkdir()
    (folder / "cut.fcpxml").write_text(_TINY, encoding="utf-8")
    (folder / "brief.txt").write_text("  Keep the story.  \n", encoding="utf-8")
    (folder / "notes.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
    (folder / "taste.json").write_text("{}", encoding="utf-8")
    (folder / "durations.json").write_text("{}\n", encoding="utf-8")
    (folder / ".hidden.mov").write_bytes(b"nope")
    nested = folder / "nested"
    nested.mkdir()
    (nested / "ignored.mp4").write_bytes(b"nope")
    drop = resolve_drop(folder)
    assert drop.fcpxml.name == "cut.fcpxml"
    assert drop.media_dir is None
    assert drop.brief == "Keep the story."
    assert drop.transcript.name == "notes.srt"
    assert drop.taste.name == "taste.json"
    assert drop.durations.name == "durations.json"
    assert output_dir_for(drop.folder) == folder.parent / "jevid-out"


def test_drop_rejects_an_empty_folder_and_a_mixed_folder(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConductorError, match="nothing to cut"):
        resolve_drop(empty)
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "cut.fcpxml").write_text(_TINY, encoding="utf-8")
    (mixed / "a.mov").write_bytes(b"clip")
    with pytest.raises(ConductorError, match="not both"):
        resolve_drop(mixed)
    videos = tmp_path / "clips"
    videos.mkdir()
    (videos / "b.mp4").write_bytes(b"clip")
    (videos / "a.mov").write_bytes(b"clip")
    drop = resolve_drop(videos)
    assert drop.fcpxml is None
    assert drop.media_dir == videos.resolve()


def test_metrics_clear_on_a_gap_plus_one_clip(tmp_path):
    source = tmp_path / "tiny.fcpxml"
    source.write_text(_TINY, encoding="utf-8")
    before = source.read_bytes()
    result = iterate(
        fcpxml=source,
        brief=BRIEF,
        out_dir=tmp_path / "out",
        max_rounds=5,
        targets=Targets(target_seconds=8, duration_tolerance=1),
    )
    assert source.read_bytes() == before
    assert result.reason == "metrics"
    assert len(result.rounds) == 1
    assert result.rounds[0].applied == 1
    handback = parse_fcpxml(result.handback)
    names = [clip.name for clip in handback.sequences[0].spine]
    assert names == ["Interview"]
    assert handback.sequences[0].duration == 8
    assert "stopped because metrics" in format_table(result)
    assert result.summary_json.is_file()
    for cut in result.rounds[0].report.payload["cuts"]:
        assert cut["pass"] == "mechanical"


def test_sample_stops_on_max_rounds(tmp_path):
    before = FIXTURE.read_bytes()
    result = iterate(
        fcpxml=FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "max",
        max_rounds=1,
        targets=Targets(max_reviews=0),
    )
    assert FIXTURE.read_bytes() == before
    assert result.reason == "max-rounds"
    assert len(result.rounds) == 1
    assert result.rounds[0].applied == 1
    assert _guest(result.handback).duration == 18
    assert result.handback.resolve() != FIXTURE.resolve()


def test_sample_runs_at_least_two_rounds_then_stops_for_no_progress(tmp_path):
    before = FIXTURE.read_bytes()
    result = iterate(
        fcpxml=FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path / "loop",
        max_rounds=5,
        targets=Targets(max_reviews=0),
    )
    assert FIXTURE.read_bytes() == before
    assert result.reason == "no-progress"
    assert len(result.rounds) >= 2
    assert result.rounds[0].applied >= 1
    assert result.rounds[1].applied == 0
    for record in result.rounds:
        assert _guest(record.handback).duration == 18
        for cut in record.report.payload["cuts"]:
            assert cut["pass"] == "mechanical"
    taste = load_taste(result.rounds[-1].report.out_taste)
    applied = taste.to_state()["feedback"]["applied"]
    assert any(item.startswith("mechanical|silence_gap|Gap|") for item in applied)
    assert "stopped because no-progress" in format_table(result)
    summary = json.loads(result.summary_json.read_text())
    assert summary["protocol"] == "cut-conductor.iterate"
    assert summary["reason"] == "no-progress"


def test_media_folder_ingests_then_iterates_without_touching_clips(tmp_path):
    clips = sorted(SELECTS.glob("*.mp4")) + sorted(SELECTS.glob("*.mov")) + sorted(SELECTS.glob("*.m4v"))
    before = {path.name: path.read_bytes() for path in clips}
    result = iterate(
        media_dir=SELECTS,
        durations_path=SELECTS / "durations.json",
        brief=BRIEF,
        out_dir=tmp_path / "selects",
        max_rounds=3,
    )
    assert result.starter is not None and result.starter.is_file()
    assert result.reason == "metrics"
    assert result.rounds[0].applied >= 1
    for path in clips:
        assert path.read_bytes() == before[path.name]
    starter_dir = tmp_path / "selects" / "starter"
    assert not (starter_dir / "selects.conductor.applied.fcpxml").exists()
    for record in result.rounds:
        for cut in record.report.payload["cuts"]:
            assert cut["pass"] == "mechanical"


def test_cli_drop_writes_the_sibling_out_folder(tmp_path):
    folder = tmp_path / "jevid-in"
    folder.mkdir()
    (folder / "tiny.fcpxml").write_text(_TINY, encoding="utf-8")
    (folder / "brief.txt").write_text(BRIEF, encoding="utf-8")
    code = main(
        [
            "iterate",
            "--drop",
            str(folder),
            "--max-rounds",
            "2",
            "--target-seconds",
            "8",
            "--duration-tolerance",
            "1",
        ]
    )
    assert code == 0
    out = tmp_path / "jevid-out"
    assert (out / "iterate.json").is_file()
    payload = json.loads((out / "iterate.json").read_text())
    assert payload["reason"] == "metrics"
    assert (folder / "tiny.fcpxml").read_text(encoding="utf-8") == _TINY


def test_cli_iterate_needs_an_input():
    code = main(["iterate"])
    assert code == 2
