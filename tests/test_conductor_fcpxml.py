"""Parser coverage for the sample interview and a project-only XML."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from conductor.errors import ConductorError
from conductor.fcpxml import parse_fcpxml, parse_xml

FIXTURE = Path("fixtures/sample_interview.fcpxml")


def test_sample_project_clips_assets_markers_and_roles():
    doc = parse_fcpxml(FIXTURE)
    assert doc.version == "1.11"
    assert set(doc.assets) == {"r2", "r3"}
    assert doc.assets["r2"].src == "file:///media/library/interview.mov"
    assert doc.assets["r2"].has_audio and doc.assets["r2"].has_video
    assert doc.formats["r1"].frame_duration == Fraction(1, 24)
    assert doc.formats["r1"].width == 1920

    sequence = doc.sequences[0]
    assert sequence.name == "Rough Cut"
    assert sequence.event == "Interview"
    assert sequence.tc_start == 0
    assert sequence.duration == Fraction(377, 6)
    assert sequence.width == 1920 and sequence.height == 1080
    assert [clip.name for clip in sequence.spine] == [
        "Cold open",
        "Gap",
        "Flash frame",
        "Guest explains",
        "B-roll static hold",
        "Button",
    ]
    cold, gap, flash, guest, broll, button = sequence.spine
    assert cold.offset == 0 and cold.start == 12 and cold.duration == 8
    assert cold.width == 1920 and cold.height == 1080
    assert gap.width is None and gap.height is None
    assert cold.audio_role == "dialogue"
    assert cold.roles == ("dialogue.dialogue-1",)
    assert cold.markers[0].value == "Keep this"
    assert cold.markers[0].note == "human marker"
    assert cold.markers[0].start == 14
    assert cold.connected_clips[0].name == "Lower third"
    assert cold.connected_clips[0].lane == 1
    assert cold.connected_clips[0].video_role == "titles"
    # The offset is on the parent's clock, which begins at its start (12s).
    assert cold.connected_clips[0].offset == 13
    assert cold.connected_clips[0].local_offset == 1
    assert cold.connected_clips[0].timeline_start == 1
    assert gap.kind == "gap" and gap.duration == Fraction(5, 2) and gap.start == 3600
    assert flash.duration == Fraction(1, 3) and flash.offset == Fraction(21, 2)
    assert guest.offset == Fraction(65, 6) and guest.audio_role == "dialogue"
    assert broll.audio_role == "effects" and broll.video_role == "video"
    assert broll.duration == 28
    assert button.duration == 6


def test_project_without_a_library_parses():
    doc = parse_xml(
        """<?xml version="1.0" encoding="UTF-8"?>
        <fcpxml version="1.9">
          <resources>
            <format id="r1" frameDuration="100/2400s" width="1280" height="720"/>
          </resources>
          <project name="Solo">
            <sequence format="r1" duration="10s" tcStart="3600s" tcFormat="NDF">
              <spine>
                <asset-clip ref="r2" offset="0s" name="A&amp;B" start="1s" duration="10s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """
    )
    sequence = doc.sequences[0]
    assert sequence.name == "Solo"
    assert sequence.event is None
    assert sequence.tc_start == 3600
    assert sequence.frame_duration == Fraction(1, 24)
    assert sequence.spine[0].name == "A&B"
    assert sequence.spine[0].start == 1


def test_not_fcpxml_is_an_error():
    with pytest.raises(ConductorError, match="fcpxml"):
        parse_xml("<xmeml version='5'></xmeml>")
