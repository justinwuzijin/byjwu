"""Colour notes come from the XML. They are never an unattended cut."""

from __future__ import annotations

from conductor.decide import judge
from conductor.fcpxml import parse_xml
from conductor.passes import PASSES, collect, is_creative
from conductor.taste import load_taste

WIDE = """
<fcpxml version="1.11">
  <resources>
    <format id="r1" frameDuration="1/24s" width="1920" height="1080"/>
    <format id="r2" frameDuration="1/24s" width="{width}" height="{height}"/>
    <asset id="a1" name="wide" format="r1" start="0s" duration="20s" hasVideo="1">
      <media-rep kind="original-media" src="file:///tmp/wide.mov"/>
    </asset>
    <asset id="a2" name="other" format="r2" start="0s" duration="20s" hasVideo="1">
      <media-rep kind="original-media" src="file:///tmp/other.mov"/>
    </asset>
  </resources>
  <project name="Colour">
    <sequence format="r1" duration="10s" tcStart="0s">
      <spine>
        <gap name="Gap" offset="0s" start="0s" duration="1s"/>
        <asset-clip ref="a2" offset="1s" name="Other" start="0s" duration="5s"{role}/>
        <asset-clip ref="a1" offset="6s" name="Wide" start="0s" duration="4s" audioRole="dialogue"/>
      </spine>
    </sequence>
  </project>
</fcpxml>
"""


def _colour(width: int, height: int, role: str = ""):
    xml = WIDE.format(width=width, height=height, role=role)
    doc = parse_xml(xml)
    return collect(doc.sequences, [], transcript_present=False, requested=["colour"])


def test_colour_is_creative_and_implemented():
    spec = PASSES["colour"]
    assert spec.implemented and spec.creative and spec.generate is not None
    assert is_creative("colour")


def test_missing_role_extreme_aspect_and_unseen_placeholder():
    found = _colour(1080, 1920)
    by_kind = {}
    for item in found:
        by_kind.setdefault(item.kind, []).append(item)
    assert [item.clip_name for item in by_kind["colour_role"]] == ["Other"]
    assert by_kind["colour_aspect"][0].clip_name == "Other"
    assert by_kind["colour_aspect"][0].signals["mismatch"] == "orientation"
    assert by_kind["colour_aspect"][0].signals["decoded_media"] is False
    assert [item.clip_name for item in by_kind["colour_unseen"]] == ["Other"]
    assert all(item.clip_name != "Gap" for item in found)
    assert all(item.pass_name == "colour" for item in found)
    assert any("not decoded" in item.reason or "did not decode" in item.reason for item in found)


def test_same_orientation_needs_a_large_aspect_gap():
    mild = _colour(2000, 1080, role=' audioRole="dialogue"')
    assert {item.kind for item in mild} == {"colour_unseen"}
    wide = _colour(1440, 1080, role=' audioRole="dialogue"')
    aspect = next(item for item in wide if item.kind == "colour_aspect")
    assert aspect.signals["mismatch"] == "ratio"
    assert aspect.signals["relative_delta"] >= 0.15


def test_mock_marks_colour_for_a_person_and_never_auto():
    found = _colour(1080, 1920)
    proposals, _receipts = judge(found, "Keep skin honest.", live=False, taste=load_taste(None))
    by_kind = {item.kind: item for item in found}
    by_id = {item.candidate_id: item for item in proposals}
    assert by_id[by_kind["colour_aspect"].id].raw_action == "escalate"
    assert by_id[by_kind["colour_aspect"].id].disposition == "escalate"
    assert by_id[by_kind["colour_role"].id].raw_action == "mark_review"
    assert by_id[by_kind["colour_role"].id].disposition == "review"
    assert by_id[by_kind["colour_unseen"].id].disposition == "review"
    assert all(item.disposition != "auto" for item in proposals)
    assert all(item.pass_name == "colour" for item in proposals)
