"""The unattended loop applies mechanical cuts and then stops."""

from __future__ import annotations

import json
from pathlib import Path

from conductor.cli import main
from conductor.fcpxml import parse_fcpxml
from conductor.ingest import file_hash
from conductor.iterate import iterate

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")
SELECTS = Path("fixtures/selects")
BRIEF = "A tight interview. Keep the guest's story, lose dead air."


def _iterate(tmp_path: Path, **kwargs):
    before = FIXTURE.read_bytes()
    result = iterate(
        fcpxml=FIXTURE,
        transcript_path=SRT,
        brief=BRIEF,
        out_dir=tmp_path,
        **kwargs,
    )
    assert FIXTURE.read_bytes() == before
    return result


def _names(path: Path) -> list[str]:
    return [clip.name for clip in parse_fcpxml(path).sequences[0].spine]


def test_two_rounds_then_no_progress(tmp_path):
    result = _iterate(tmp_path, max_rounds=5)
    assert result.stop_reason == "no-progress"
    assert len(result.rounds) == 2
    assert result.needs_human is False
    first, second = result.rounds
    assert first["applied_ids"]
    assert all(cut["pass"] == "mechanical" and cut["kind"] == "silence_gap" for cut in first["cuts"])
    assert second["applied_ids"] == []
    assert second["applied_fcpxml"] is None
    assert "Gap" not in _names(Path(first["applied_fcpxml"]))
    assert "Gap" not in _names(Path(second["source"]))
    payload = json.loads(Path(second["json"]).read_text())
    assert payload["taste"]["feedback"]["accepts"] >= 1
    assert any(event.get("kind") == "silence_gap" for event in payload["taste"]["feedback"]["recent"])
    summary = json.loads(result.out_json.read_text())
    assert summary["protocol"] == "cut-conductor.iterate"
    assert summary["stop_reason"] == "no-progress"
    assert [item["candidate_id"] for item in summary["applied"]] == first["applied_ids"]


def test_stops_on_max_rounds_while_a_cut_still_lands(tmp_path):
    result = _iterate(tmp_path, max_rounds=1)
    assert result.stop_reason == "max-rounds"
    assert result.needs_human is True
    assert result.human_reasons == ["max-rounds"]
    assert len(result.rounds) == 1
    assert result.rounds[0]["applied_ids"]
    assert not (tmp_path / "v2").exists()
    assert "Gap" not in _names(Path(result.rounds[0]["applied_fcpxml"]))


def test_stops_when_duration_lands_in_the_window(tmp_path):
    result = _iterate(tmp_path, max_rounds=5, target_seconds=60.333333, tolerance=0.01)
    assert result.stop_reason == "metrics"
    assert result.cleared == ["duration"]
    assert len(result.rounds) == 1
    assert result.rounds[0]["applied_ids"]
    assert abs(result.rounds[0]["metrics"]["duration_seconds"] - 60.333333) <= 0.01
    assert result.rounds[0]["metrics"]["silence_seconds"] == 0


def test_already_on_target_does_not_cut(tmp_path):
    result = _iterate(tmp_path, max_rounds=5, target_seconds=62.833333, tolerance=0.01)
    assert result.stop_reason == "metrics"
    assert len(result.rounds) == 1
    assert result.rounds[0]["applied_ids"] == []
    assert result.rounds[0]["applied_fcpxml"] is None
    assert "Gap" in _names(Path(result.rounds[0]["shadow"]))


def test_media_folder_reuses_the_starter_and_cuts_the_flash(tmp_path):
    before = {path: file_hash(path) for path in SELECTS.rglob("*") if path.is_file()}
    result = iterate(
        media=SELECTS,
        durations_path=SELECTS / "durations.json",
        brief=BRIEF,
        out_dir=tmp_path,
        max_rounds=5,
    )
    assert {path: file_hash(path) for path in before} == before
    assert result.starter is not None
    assert _names(result.starter) == ["a_interview", "b_flash", "c_button"]
    assert result.stop_reason == "no-progress"
    assert len(result.rounds) == 2
    assert result.rounds[0]["cuts"][0]["kind"] == "short_clip"
    assert _names(Path(result.rounds[0]["applied_fcpxml"])) == ["a_interview", "c_button"]
    assert all(cut["pass"] == "mechanical" for cut in result.applied)


def test_colour_escalate_asks_for_a_person_and_is_not_cut(tmp_path):
    xml = tmp_path / "vertical.fcpxml"
    xml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <format id="r2" frameDuration="1/24s" width="1080" height="1920"/>
            <asset id="a1" name="vert" format="r2" start="0s" duration="8s" hasVideo="1">
              <media-rep kind="original-media" src="file:///tmp/vert.mov"/>
            </asset>
          </resources>
          <project name="Vertical">
            <sequence format="r1" duration="8s" tcStart="0s">
              <spine>
                <asset-clip ref="a1" offset="0s" name="Vertical" start="0s" duration="8s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """
    )
    result = iterate(fcpxml=xml, brief=BRIEF, out_dir=tmp_path / "out", max_rounds=3)
    assert result.stop_reason == "no-progress"
    assert result.rounds[0]["applied_ids"] == []
    assert result.needs_human is True
    assert result.human_reasons == ["escalate"]
    assert result.rounds[0]["metrics"]["escalate_count"] >= 1
    assert _names(Path(result.rounds[0]["shadow"])) == ["Vertical"]


def test_cli_prints_the_table(tmp_path, capsys):
    code = main(
        [
            "iterate",
            "--fcpxml",
            str(FIXTURE),
            "--transcript",
            str(SRT),
            "--brief",
            BRIEF,
            "--out-dir",
            str(tmp_path / "cli"),
            "--max-rounds",
            "1",
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "stop max-rounds" in output
    assert "human  max-rounds" in output
    assert "duration" in output
    missing = main(["iterate", "--brief", BRIEF, "--out-dir", str(tmp_path / "nope")])
    assert missing == 2
    both = main(
        [
            "iterate",
            "--fcpxml",
            str(FIXTURE),
            "--media",
            str(SELECTS),
            "--brief",
            BRIEF,
            "--out-dir",
            str(tmp_path / "both"),
        ]
    )
    assert both == 2
