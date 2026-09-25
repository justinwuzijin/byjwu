"""Real Final Cut export: swiss-italy, a ~39 minute travel timeline.

The file has no media and no transcript. These tests lock the parser, the
bare-versus-covered gap split, and the rule that iterate may lift only the
bare primary.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest

from conductor.apply import Deletion, apply_edits
from conductor.decide import _deletion, judge
from conductor.errors import ConductorError
from conductor.fcpxml import parse_fcpxml, parse_xml, write_document
from conductor.markers import _append_marker, marker_order_violations
from conductor.iterate import iterate
from conductor.passes import collect
from conductor.run import analyze
from conductor.taste import load_taste

FIXTURE = Path("fixtures/swiss-italy.fcpxml")
BRIEF = "A travel vlog. Keep the journey, lose dead air and flash frames."

_KEPT = (
    "asset-clip",
    "clip",
    "video",
    "gap",
    "audio",
    "filter-video",
    "filter-audio",
    "keyframe",
    "adjust-voiceIsolation",
    "bookmark",
    "conform-rate",
    "audio-channel-source",
)


def _signature(element: ET.Element):
    text = (element.text or "").strip()
    children = tuple(_signature(child) for child in list(element))
    tag = element.tag.rsplit("}", 1)[-1]
    return (tag, tuple(sorted(element.attrib.items())), text, children)


def _counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    tags = Counter(element.tag.rsplit("}", 1)[-1] for element in root.iter())
    return {name: tags[name] for name in _KEPT}


def _on_sequence_grid(path: Path) -> None:
    """Spine timeline times land on the sequence frame.

    Connected offsets stay in the parent's timebase. Final Cut sometimes
    stores those on a different denominator, and a ripple that does not
    rewrite them must leave that string alone.
    """
    document = parse_fcpxml(path)
    sequence = document.sequences[0]
    frame = sequence.frame_duration
    previous = None
    for clip in sequence.spine:
        for value in (clip.offset, clip.duration, clip.start):
            units = value / frame
            assert units.denominator == 1, (clip.name, value, units)
        if previous is not None:
            assert clip.offset == previous
        previous = clip.offset + clip.duration


def test_swiss_italy_parses_the_real_spine():
    document = parse_fcpxml(FIXTURE)
    assert document.version == "1.14"
    sequence = document.sequences[0]
    assert sequence.name == "swiss-italy"
    assert sequence.frame_duration == Fraction(1001, 24000)
    assert sequence.width == 3840 and sequence.height == 2160
    assert len(sequence.spine) == 200
    assert sum(1 for clip in sequence.spine if clip.kind == "gap") == 1
    assert sum(1 for clip in sequence.spine if clip.kind == "asset-clip") == 161
    gap = next(clip for clip in sequence.spine if clip.kind == "gap")
    laned = [clip for clip in gap.connected_clips if clip.lane is not None]
    assert len(laned) == 8
    assert {clip.anchor for clip in laned} == {"media"}
    assert laned[0].local_offset > 90
    # Child offset is in the gap's source time. Adding it to the gap offset
    # lands past the end of the sequence; subtracting the gap start does not.
    assert laned[0].timeline_start < sequence.duration
    assert abs(float(laned[0].timeline_start) - 2064.479) < 0.01
    assert abs(float(laned[-1].timeline_end) - 2189.187) < 0.01
    compound = sequence.spine[1]
    assert compound.kind == "clip"
    assert [clip.lane for clip in compound.connected_clips] == [-1]
    audio = compound.connected_clips[0]
    assert abs(float(audio.timeline_start) - 7.549) < 0.01
    assert float(audio.duration) < 15
    assert all(float(clip.duration) < 30 for clip in _descendants(audio))
    portrait = next(clip for clip in sequence.spine if clip.width == 2160 and clip.height == 3840)
    assert portrait.name == "C8432"


def test_write_with_no_cuts_matches_the_parsed_tree(tmp_path):
    original = ET.parse(FIXTURE).getroot()
    document = parse_fcpxml(FIXTURE)
    dest = tmp_path / "round.fcpxml"
    write_document(document.tree, dest)
    assert _signature(original) == _signature(ET.parse(dest).getroot())
    assert dest.read_text(encoding="utf-8").startswith("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE fcpxml>\n")


def test_candidates_are_useful_and_only_bare_gaps_are_automatic():
    document = parse_fcpxml(FIXTURE)
    found = collect(document.sequences, [], transcript_present=False)
    by_kind = Counter(item.kind for item in found)
    assert by_kind == Counter(
        {
            "silence_gap": 2,
            "covered_gap": 1,
            "long_static": 3,
            "source_reuse": 25,
            "rhythm_shift": 2,
            "rate_mix": 1,
            "colour_aspect": 1,
            "colour_role": 4,
            "colour_unseen": 1,
            "untrimmed_run": 1,
            "silent_card": 2,
            "music_tail": 1,
        }
    )

    proposals, _receipts = judge(
        found, BRIEF, live=False, taste=load_taste(None)
    )
    by_id = {item.candidate_id: item for item in proposals}
    eligible = [item for item in found if by_id[item.id].disposition == "auto"]
    assert [item.kind for item in eligible] == ["silence_gap", "silence_gap"]
    assert all(item.signals.get("bare") and item.signals.get("partial") for item in eligible)
    covered = next(item for item in found if item.kind == "covered_gap")
    for item in eligible:
        assert item.timeline_end <= covered.timeline_start or item.timeline_start >= covered.timeline_end
    for kind in (
        "covered_gap",
        "source_reuse",
        "rhythm_shift",
        "rate_mix",
        "long_static",
        "untrimmed_run",
        "silent_card",
        "music_tail",
    ):
        assert all(by_id[item.id].disposition == "review" for item in found if item.kind == kind)
    aspect = next(item for item in found if item.kind == "colour_aspect")
    assert by_id[aspect.id].disposition == "escalate"
    assert aspect.signals.get("rotation") == "90"
    assert "rotated 90" in aspect.reason and "scaled 1.8" in aspect.reason
    stringout = next(item for item in found if item.kind == "untrimmed_run")
    assert stringout.signals["shot_count"] == 35
    assert abs(stringout.signals["average_seconds"] - 34.6) < 0.2
    cards = [item for item in found if item.kind == "silent_card"]
    assert [round(float(item.timeline_start), 0) for item in cards] == [114, 1862]
    music = next(item for item in found if item.kind == "music_tail")
    assert abs(music.signals["tail_seconds"] - 24.1) < 0.2
    notes = "\n".join(
        analyze_notes(document, found, proposals)
    )
    assert "32:48" in notes and "36:29" in notes
    assert "34:24" in notes
    assert "2160×3840" in notes
    assert "reprises" in notes
    assert "29.97" in notes
    assert "whole source clip" in notes
    assert "Silent generator" in notes
    assert "rotated 90" in notes
    assert "before the picture" in notes
    with pytest.raises(ConductorError, match="note"):
        _deletion(covered, "remove", Fraction(4))


def analyze_notes(document, found, proposals):
    from conductor.notes import editor_notes

    return editor_notes(
        sequences=document.sequences,
        candidates=found,
        proposals=proposals,
        transcript_present=False,
    )


def test_iterate_lifts_only_the_bare_gap_and_keeps_the_timeline(tmp_path):
    before = _counts(FIXTURE)
    result = iterate(
        fcpxml=FIXTURE,
        brief=BRIEF,
        out_dir=tmp_path,
        max_rounds=4,
    )
    assert result.stop_reason == "no-progress"
    assert len(result.rounds) == 2
    assert result.needs_human is True
    assert result.human_reasons == ["escalate"]
    assert result.applied
    assert {item["kind"] for item in result.applied} == {"silence_gap"}
    assert all(item["pass"] == "mechanical" for item in result.applied)
    removed = sum(item["end_seconds"] - item["start_seconds"] for item in result.applied)
    assert 100 < removed < 120

    applied = Path(result.rounds[0]["applied_fcpxml"])
    assert _counts(applied) == before
    _on_sequence_grid(applied)
    document = parse_fcpxml(applied)
    sequence = document.sequences[0]
    assert len(sequence.spine) == 200
    gap = next(clip for clip in sequence.spine if clip.kind == "gap")
    laned = [clip for clip in gap.connected_clips if clip.lane is not None]
    assert [clip.name for clip in laned] == [
        "C8358",
        "C8358",
        "C8358",
        "C8359",
        "C8363",
        "C8363",
        "C8361",
        "C8362",
    ]
    assert laned[0].local_offset == 0
    assert abs(float(gap.duration) - 124.7) < 0.1
    assert _gap_lane_offsets(FIXTURE) == _gap_lane_offsets(applied)
    shadow = Path(result.rounds[0]["markdown"])
    text = shadow.read_text(encoding="utf-8")
    assert "## Editor's notes" in text
    assert "32:48" in text
    assert "silence cuts" in text
    again = collect(parse_fcpxml(applied).sequences, [], transcript_present=False)
    assert [item.kind for item in again if item.kind == "silence_gap"] == []


def _gap_lane_offsets(path: Path) -> list[str | None]:
    root = ET.parse(path).getroot()
    spine = next(element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "spine")
    gap = next(child for child in spine if child.tag == "gap")
    return [child.get("offset") for child in gap if child.get("lane")]


def test_media_time_trim_reoffsets_the_connected_clip(tmp_path):
    path = tmp_path / "gap.fcpxml"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE fcpxml>
        <fcpxml version="1.11">
          <resources>
            <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
            <asset id="r2" name="broll" format="r1" start="0s" duration="30s" hasVideo="1"/>
          </resources>
          <project name="Gap">
            <sequence format="r1" duration="40s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="Before" start="0s" duration="10s">
                  <filter-video ref="r9" name="Color">
                    <param name="Brightness" key="1" value="0"/>
                  </filter-video>
                </asset-clip>
                <gap name="Gap" offset="10s" start="3600s" duration="20s">
                  <asset-clip ref="r2" lane="1" offset="3610s" name="B-roll" start="0s" duration="5s">
                    <adjust-voiceIsolation amount="50"/>
                  </asset-clip>
                </gap>
                <asset-clip ref="r2" offset="30s" name="After" start="1s" duration="10s"/>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """,
        encoding="utf-8",
    )
    report = analyze(
        path,
        brief=BRIEF,
        out_dir=tmp_path / "out",
        apply=True,
        min_confidence=0.8,
        passes=["mechanical"],
    )
    by_id = {item["id"]: item for item in report.candidates}
    assert [by_id[item["candidate_id"]]["kind"] for item in report.payload["cuts"]] == [
        "silence_gap",
        "silence_gap",
    ]
    applied = parse_fcpxml(report.out_applied)
    spine = applied.sequences[0].spine
    assert [clip.name for clip in spine] == ["Before", "Gap", "After"]
    assert spine[0].offset == 0 and spine[0].duration == 10
    assert spine[1].offset == 10 and spine[1].duration == 5 and spine[1].start == 3610
    assert spine[1].connected_clips[0].name == "B-roll"
    assert spine[1].connected_clips[0].offset == 3610
    assert spine[1].connected_clips[0].local_offset == 0
    assert spine[2].offset == 15 and spine[2].start == 1
    gap = spine[1].element
    audio = next(child for child in gap if child.tag == "asset-clip")
    assert audio.find("adjust-voiceIsolation").get("amount") == "50"
    assert spine[0].element.find("filter-video").get("name") == "Color"
    _on_sequence_grid(report.out_applied)


def test_edit_local_title_inside_a_trim_keeps_its_place(tmp_path):
    document = parse_xml(
        """
        <fcpxml version="1.11">
          <resources><format id="r1" frameDuration="1/24s"/></resources>
          <project name="Talk">
            <sequence format="r1" duration="8s" tcStart="0s">
              <spine>
                <asset-clip ref="r2" offset="0s" name="Talk" start="12s" duration="8s">
                  <asset-clip ref="r3" lane="1" offset="3s" name="Title" start="0s" duration="2s"/>
                </asset-clip>
              </spine>
            </sequence>
          </project>
        </fcpxml>
        """
    )
    title = document.sequences[0].spine[0].connected_clips[0]
    assert title.anchor == "edit" and title.timeline_start == 3
    apply_edits(
        document,
        [
            Deletion(
                candidate_id="c0001",
                sequence="Talk",
                start=Fraction(0),
                end=Fraction(2),
                action="remove",
                pass_name="mechanical",
            )
        ],
    )
    dest = tmp_path / "trimmed.fcpxml"
    write_document(document.tree, dest)
    trimmed = parse_fcpxml(dest).sequences[0].spine[0]
    assert trimmed.duration == 6 and trimmed.start == 14 and trimmed.offset == 0
    assert trimmed.connected_clips[0].name == "Title"
    assert trimmed.connected_clips[0].offset == 1
    assert trimmed.connected_clips[0].duration == 2


def _descendants(clip):
    yield clip
    for child in clip.connected_clips:
        yield from _descendants(child)


def test_wholesale_gap_removal_keeps_the_connected_broll(tmp_path):
    document = parse_fcpxml(FIXTURE)
    sequence = document.sequences[0]
    gap = next(clip for clip in sequence.spine if clip.kind == "gap")
    result = apply_edits(
        document,
        [
            Deletion(
                candidate_id="c-whole",
                sequence=sequence.name,
                start=gap.timeline_start,
                end=gap.timeline_end,
                action="remove",
                pass_name="mechanical",
            )
        ],
    )
    assert any("connected clip covers" in note for note in result.warnings)
    removed = sum(item["end_seconds"] - item["start_seconds"] for item in result.cuts)
    assert 100 < removed < 120
    dest = tmp_path / "shielded.fcpxml"
    write_document(document.tree, dest)
    applied = parse_fcpxml(dest)
    kept = next(clip for clip in applied.sequences[0].spine if clip.kind == "gap")
    laned = [clip for clip in kept.connected_clips if clip.lane is not None]
    assert [clip.name for clip in laned] == [
        "C8358",
        "C8358",
        "C8358",
        "C8359",
        "C8363",
        "C8363",
        "C8361",
        "C8362",
    ]
    assert laned[0].local_offset == 0
    assert abs(float(kept.duration) - 124.7) < 0.1
    again = collect(applied.sequences, [], transcript_present=False)
    assert [item.kind for item in again if item.kind == "silence_gap"] == []


def test_markers_are_inserted_before_filters(tmp_path):
    document = parse_fcpxml(FIXTURE)
    montage = next(clip for clip in document.sequences[0].spine if clip.name == "C8379")
    assert montage.element is not None
    tags_before = [child.tag for child in montage.element]
    assert "audio-channel-source" in tags_before and "filter-video" in tags_before
    _append_marker(
        montage.element,
        start=montage.start,
        duration=document.sequences[0].frame_duration,
        value="CC note",
        note="Cut Conductor shadow proposal | check",
        completed="0",
    )
    tags = [child.tag for child in montage.element]
    assert tags.index("marker") < tags.index("audio-channel-source")
    assert tags.index("marker") < tags.index("filter-video")
    assert marker_order_violations(document.tree.getroot()) == []
    report = analyze(FIXTURE, brief=BRIEF, out_dir=tmp_path, passes=None)
    written = ET.parse(report.out_fcpxml).getroot()
    assert marker_order_violations(written) == []
    assert any(node.tag == "marker" and node.get("value", "").startswith("CC ") for node in written.iter())
