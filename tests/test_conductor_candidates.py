"""Heuristic candidates, pass filters, and the future-pass extension point."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from conductor.candidates import (
    FILLER_MAX,
    FILLER_MIN,
    LONG_STATIC,
    PAUSE,
    SHORT_CLIP,
    SILENCE_GAP,
)
from conductor.errors import ConductorError
from conductor.fcpxml import parse_fcpxml, parse_xml
from conductor.passes import PASSES, collect, register_pass
from conductor.transcript import parse_cues
from cutmcp.extract import is_trivial_filler

FIXTURE = Path("fixtures/sample_interview.fcpxml")
SRT = Path("fixtures/sample_interview.srt")


def _collect(xml: str, cues: str = "", passes=None, transcript: bool = True):
    doc = parse_xml(xml)
    parsed = parse_cues(cues) if cues else []
    return collect(
        doc.sequences,
        parsed,
        transcript_present=transcript and bool(cues),
        requested=passes,
    )


def test_filler_regex_matches_cutmcp_and_ignores_load_bearing_words():
    assert is_trivial_filler("Um.")
    assert is_trivial_filler("You know.")
    assert not is_trivial_filler("I like the migration.")
    assert not is_trivial_filler("Yeah, the replica lagged.")
    assert FILLER_MIN == Fraction("0.25")
    assert FILLER_MAX == 3
    assert PAUSE == Fraction("0.80")
    assert SILENCE_GAP == Fraction("1.25")
    assert SHORT_CLIP == Fraction("0.45")
    assert LONG_STATIC == 20


def test_sample_kinds_and_passes():
    cues = SRT.read_text()
    doc = parse_fcpxml(FIXTURE)
    found = collect(doc.sequences, parse_cues(cues), transcript_present=True)
    by_kind = {}
    for item in found:
        by_kind.setdefault(item.kind, []).append(item)
    silence = [item for item in found if item.kind == "silence_gap"]
    assert [item.pass_name for item in silence] == ["mechanical"]
    assert silence[0].id == "c0002" and silence[0].span == "clip"
    colour = [item for item in found if item.pass_name == "colour"]
    assert [item.kind for item in colour] == ["colour_unseen"]
    assert "not decoded" in colour[0].reason or "did not decode" in colour[0].reason
    assert any(item.kind == "short_clip" and item.pass_name == "mechanical" for item in found)
    assert any(item.kind == "long_static" and item.pass_name == "pacing" for item in found)
    fillers = [item for item in found if item.kind == "filler_pause"]
    assert {item.transcript for item in fillers} >= {"Um.", "You know."}
    assert all(item.pass_name == "dialogue" for item in fillers)
    assert any(item.signals.get("pure_filler") for item in fillers)
    assert any(item.label == "pause" and item.signals.get("adjacent_filler") for item in fillers)
    assert "Cold open" not in {item.clip_name for item in found if item.pass_name != "colour"}


def test_mechanical_pass_skips_dialogue_and_pacing():
    doc = parse_fcpxml(FIXTURE)
    found = collect(
        doc.sequences,
        parse_cues(SRT.read_text()),
        transcript_present=True,
        requested=["mechanical"],
    )
    assert {item.pass_name for item in found} == {"mechanical"}
    assert {item.kind for item in found} <= {"silence_gap", "short_clip"}


def test_long_talking_head_is_not_a_hold():
    words = " ".join(["migration"] * 40)
    found = _collect(
        """
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Talk">
            <sequence format="r1" duration="22s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="Answer" start="0s" duration="22s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        cues=f"1\n00:00:00,000 --> 00:00:22,000\n{words}\n",
        passes=["pacing"],
    )
    assert found == []


def test_long_clip_without_transcript_needs_a_name_or_45_seconds():
    quiet = """
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Hold">
            <sequence format="r1" duration="30s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="{name}" start="0s" duration="30s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """
    named = _collect(quiet.format(name="Slate hold"), transcript=False, passes=["pacing"])
    plain = _collect(quiet.format(name="Answer"), transcript=False, passes=["pacing"])
    assert len(named) == 1 and named[0].signals["name_hint"] is True
    assert plain == []


def test_a_gap_under_a_connected_clip_is_not_silence():
    found = _collect(
        """
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s" width="1920" height="1080"/></resources>
          <project name="Title">
            <sequence format="r1" duration="6s" tcStart="0s">
              <spine>
                <gap name="Gap" offset="0s" start="100s" duration="6s">
                  <title lane="1" offset="100s" name="Chapter" start="0s" duration="6s"/>
                </gap>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        transcript=False,
    )
    assert [item.kind for item in found if item.pass_name == "mechanical"] == []
    covered = [item for item in found if item.kind == "covered_gap"]
    assert len(covered) == 1
    assert covered[0].span == "note"
    assert covered[0].pass_name == "pacing"
    assert covered[0].signals["do_not_cut"] is True


def test_uncovered_head_of_a_real_gap_is_silence_and_the_covered_stretch_is_not():
    found = _collect(
        """
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Gap">
            <sequence format="r1" duration="20s" tcStart="0s">
              <spine>
                <gap name="Gap" offset="0s" start="3600s" duration="20s">
                  <asset-clip ref="r2" lane="1" offset="3610s" name="B-roll" start="0s" duration="5s"/>
                </gap>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        transcript=False,
        passes=["mechanical", "pacing"],
    )
    silence = [item for item in found if item.kind == "silence_gap"]
    covered = [item for item in found if item.kind == "covered_gap"]
    assert [item.duration for item in silence] == [10, 5]
    assert all(item.pass_name == "mechanical" and item.span == "subrange" for item in silence)
    assert len(covered) == 1 and covered[0].duration == 5 and covered[0].span == "note"


def test_forty_five_seconds_among_similar_shots_is_not_a_hold():
    clips = "\n".join(
        f'<asset-clip ref="r2" offset="{index * 40}s" name="Shot {index}" start="0s" duration="40s"/>'
        for index in range(5)
    )
    found = _collect(
        f"""
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Even">
            <sequence format="r1" duration="200s" tcStart="0s">
              <spine>{clips}</spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        transcript=False,
        passes=["pacing"],
    )
    assert [item for item in found if item.kind == "long_static"] == []


def test_a_shot_four_times_its_neighbors_is_a_hold():
    durations = [8, 8, 8, 8, 80, 8, 8, 8, 8]
    offset = 0
    clips = []
    for index, duration in enumerate(durations):
        clips.append(
            f'<asset-clip ref="r2" offset="{offset}s" name="Shot {index}" start="0s" duration="{duration}s"/>'
        )
        offset += duration
    found = _collect(
        f"""
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Hold">
            <sequence format="r1" duration="{offset}s" tcStart="0s">
              <spine>{''.join(clips)}</spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        transcript=False,
        passes=["pacing"],
    )
    holds = [item for item in found if item.kind == "long_static"]
    assert [item.clip_name for item in holds] == ["Shot 4"]
    assert holds[0].signals["asl_ratio"] >= 4


def test_repeated_source_range_is_a_note():
    found = _collect(
        """
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s"/>
            <asset id="r2" name="drone" format="r1" start="0s" duration="30s" hasVideo="1"/>
          </resources>
          <project name="Reprise">
            <sequence format="r1" duration="12s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="First" start="10s" duration="8s"/>
                <asset-clip ref="r2" offset="8s" name="Again" start="12s" duration="4s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        transcript=False,
        passes=["pacing"],
    )
    reused = [item for item in found if item.kind == "source_reuse"]
    assert len(reused) == 1
    assert reused[0].clip_name == "Again"
    assert reused[0].span == "note"
    assert reused[0].signals["fraction_of_shorter"] == 1


def test_implicit_hole_is_a_silence_candidate():
    found = _collect(
        """
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Hole">
            <sequence format="r1" duration="12s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="A" start="0s" duration="4s"/>
                <asset-clip ref="r2" offset="7s" name="B" start="0s" duration="5s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        transcript=False,
        passes=["mechanical"],
    )
    assert len(found) == 1
    assert found[0].span == "hole"
    assert found[0].duration == 3


def test_vtt_cues_parse():
    cues = parse_cues(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.500 align:start\n<v Guest>Um.\n"
    )
    assert len(cues) == 1
    assert cues[0].text == "Um."
    assert cues[0].start == Fraction(1, 1)
    assert cues[0].end == Fraction(5, 2)


def test_unknown_and_reserved_passes():
    with pytest.raises(ConductorError, match="unknown pass"):
        collect([], [], transcript_present=False, requested=["nope"])
    with pytest.raises(ConductorError, match="not implemented"):
        collect([], [], transcript_present=False, requested=["story"])


def test_register_pass_is_the_extension_point():
    original = PASSES["story"]

    def _empty(sequences, cues, transcript_present):
        return []

    try:
        register_pass(
            "story",
            kinds=frozenset(),
            creative=True,
            implemented=True,
            summary="test double",
            generate=_empty,
        )
        found = collect([], [], transcript_present=False, requested=["story"])
        assert found == []
    finally:
        PASSES["story"] = original
